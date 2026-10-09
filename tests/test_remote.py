from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bookkeeping import cli, hosted, remote
from bookkeeping.ledger.migrate import migrate_beancount_to_store
from bookkeeping.ledger.projections import render_store_ledger
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.remote_contract import command_spec, parse_command


TOKEN = "synthetic-remote-token"
LEGACY_LEDGER = b'''option "title" "Synthetic remote migration"
option "operating_currency" "USD"
2026-01-01 open Assets:Bank:Checking USD
2026-01-01 open Expenses:Software USD
2026-01-02 * "Synthetic subscription"
  source-id: "legacy-parity-1"
  Assets:Bank:Checking -19.00 USD
  Expenses:Software 19.00 USD
'''


def artifact(path: str, data: bytes = b"synthetic output\n") -> dict:
    return {"path": path, "data_base64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}


class API:
    def __init__(self) -> None:
        self.calls = []
        self.revision = 7
        self.results = {}
        self.artifacts = []
        self.exit_code = 0
        self.stdout = "remote output\n"
        self.stderr = ""
        self.drop = False
        self.status = 200
        self.file = b"original notes\n"
        self.command_error = None
        self.result_revision = None
        self.command_handler = None
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(data) if data else None
                api.calls.append({"method": self.command, "path": self.path, "body": body,
                                  "key": self.headers.get("Idempotency-Key"), "authorization": self.headers.get("Authorization")})
                status = api.status
                if self.command == "GET":
                    if "/engine/files" in self.path:
                        query = parse_qs(urlsplit(self.path).query)
                        result = {**artifact(query["path"][0], api.file), "books_revision": api.revision} if query else {
                            "files": [{"path": "context/notes.md", "size": len(api.file), "sha256": hashlib.sha256(api.file).hexdigest()}],
                            "books_revision": api.revision,
                        }
                    else:
                        result = {"books_revision": api.revision}
                elif self.command == "PUT":
                    key = self.headers.get("Idempotency-Key")
                    if key in api.results:
                        result = api.results[key]
                    elif body["expected_books_revision"] != api.revision:
                        status, result = 409, {"error": {"code": "REVISION_CONFLICT"}}
                    else:
                        api.file = base64.b64decode(body["data_base64"])
                        api.revision += 1
                        result = {"path": body["path"], "books_revision": api.revision, "sha256": hashlib.sha256(api.file).hexdigest()}
                        api.results[key] = result
                else:
                    key = self.headers.get("Idempotency-Key")
                    if key in api.results:
                        result = api.results[key]
                    elif api.command_error is not None:
                        status, result = 422, {"error": api.command_error}
                    elif body["expected_books_revision"] != api.revision:
                        status, result = 409, {"error": {"code": "REVISION_CONFLICT"}}
                    else:
                        if api.result_revision is not None:
                            api.revision = api.result_revision
                        if api.command_handler is not None:
                            result = api.command_handler(body, key)
                            api.revision = result["books_revision"]
                        else:
                            result = {"command_id": key, "exit_code": api.exit_code, "stdout": api.stdout,
                                      "stderr": api.stderr, "state_committed": True, "books_revision": api.revision,
                                      "artifacts": api.artifacts}
                        api.results[key] = result
                if api.drop and self.command != "GET":
                    self.close_connection = True
                    return
                raw = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = respond

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.endpoint = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.entity = self.directory / "company"
        self.entity.mkdir()
        self.api = API()
        self.addCleanup(self.api.close)
        env = patch.dict(os.environ, {"BOOKS_API_TOKEN": TOKEN}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        dotenv = patch.object(cli, "load_dotenv")
        dotenv.start()
        self.addCleanup(dotenv.stop)
        code, _, _ = self.invoke("hosted", "configure", "--endpoint", self.api.endpoint, "--company", "company-A",
                                 "--allow-localhost", "--entity", str(self.entity))
        self.assertEqual(code, 0)
        self.config = self.entity / remote.CONFIG_NAME

    def invoke(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def receipts(self):
        return sorted((self.entity / ".slashbooks-remote-receipts").glob("*.json"))

    def command(self, *argv):
        return self.invoke(*argv, "--entity", str(self.entity))

    def _ledger_wire_handler(self, body, key):
        """Exercise real core accounting in a synthetic HTTP service, without PG."""
        args, _, path = parse_command(body["argv"])
        server = self.directory / "synthetic-ledger-service"
        server.mkdir(exist_ok=True)
        inputs = {item["path"]: base64.b64decode(item["data_base64"], validate=True) for item in body["inputs"]}
        result = {"command_id": key, "exit_code": 0, "stdout": "", "stderr": "",
                  "state_committed": False, "books_revision": self.api.revision, "artifacts": []}
        if path == ("ledger", "migrate"):
            if "@entity/books.beancount" in inputs:
                (server / "books.beancount").write_bytes(inputs["@entity/books.beancount"])
            store = server / ("alternate.sqlite" if args.store else "ledger.sqlite")
            migrated = migrate_beancount_to_store(server, store_path=store, dry_run=args.dry_run, force=args.force)
            if not migrated.success:
                return {**result, "exit_code": 1, "stdout": migrated.error_message + "\n"}
            result["stdout"] = f"entries: {migrated.counts.entries}\n"
            result["state_committed"] = not args.dry_run
            result["books_revision"] += int(result["state_committed"])
            if args.store is not None and not args.dry_run:
                result["artifacts"] = [artifact(str(args.store), store.read_bytes())]
            return result
        if path == ("ledger", "snapshot"):
            store = server / "uploaded.sqlite"
            store.write_bytes(inputs[str(args.store)])
            result["artifacts"] = [artifact(str(args.output), render_store_ledger(store).encode())]
            return result
        raise AssertionError(f"Unexpected synthetic ledger command: {path}")

    def test_binding_is_private_without_creating_local_books(self):
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.config.read_text())["tokenref"], "env:BOOKS_API_TOKEN")
        self.assertEqual(self.api.calls, [])
        self.assertFalse((self.entity / "ledger.sqlite").exists())
        self.assertFalse((self.entity / "entity.json").exists())

    def test_all_parser_families_route_through_shared_contract(self):
        source = self.directory / "input.json"
        source.write_text("{}")
        folder = self.directory / "exports"
        folder.mkdir()
        (folder / "sample.csv").write_text("date,amount\n")
        paths = {"entity": self.entity, "entity_path": self.entity, "path": self.entity,
                 "input": source, "file": source, "folder": folder, "qb_folder": folder,
                 "output": self.directory / "snapshot.json"}
        cases = []

        def walk(parser, route, inherited):
            actions = inherited + [a for a in parser._actions if not isinstance(a, argparse._SubParsersAction)]
            selectors = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
            if selectors:
                for name, child in selectors[0].choices.items():
                    if route or name not in {"cloud", "hosted"}:
                        walk(child, route + [name], actions)
                return
            argv = list(route)
            for action in actions:
                if action.dest == "help" or not action.required:
                    continue
                value = paths.get(action.dest, None)
                if action.dest == "output":
                    value = self.directory / ("_".join(route) + ".json")
                if value is None:
                    value = action.choices[0] if action.choices else "1" if action.type is int else "2026-01-01" if "date" in action.dest else "synthetic"
                if action.option_strings:
                    argv.extend([action.option_strings[0], str(value)])
                else:
                    argv.append(str(value))
            if route[:2] == ["connector", "mercury"] and route[-1] in {"download", "pull"}:
                argv.append("--all-accounts")
            cases.append(argv)

        walk(cli.build_parser(), [], [])
        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, "cwd", return_value=self.entity))
            runners = [stack.enter_context(patch.object(module, "run", side_effect=AssertionError("local fallback"))) for module in (
                cli.demo_module, cli.entity_module, cli.ingest_module, cli.ledger_migrate_module,
                cli.quickbooks_module, cli.statements_module, cli.reconcile_module, cli.compare_module,
                cli.queue_module, cli.workbook_module, cli.csvsource_module,
            )]
            for argv in cases:
                with self.subTest(argv=argv):
                    self.api.artifacts = [artifact("@output/output/" + Path(argv[argv.index("--output") + 1]).name)] if "--output" in argv else []
                    if argv[0] == "quarterly-review":
                        stem = argv[argv.index("--year") + 1] + "-" + argv[argv.index("--quarter") + 1]
                        self.api.artifacts = [artifact(f"@entity/reports/quarterly/{stem}.{suffix}") for suffix in ("json", "txt")]
                    code, out, err = self.invoke(*argv)
                    self.assertEqual(code, 0, err)
                    self.assertEqual(out, self.api.stdout)
                    sent = self.api.calls[-1]["body"]["argv"]
                    self.assertEqual(command_spec(sent)["path"], command_spec(argv)["path"])
                    self.assertNotIn(str(self.entity), sent)
            self.assertTrue(all(not runner.called for runner in runners))
        self.assertGreater(len(cases), 60)

    def test_no_binding_keeps_local_dispatch(self):
        with patch.object(cli.queue_module, "run", return_value=23) as local:
            self.assertEqual(self.invoke("queue", "summary", "--entity", str(self.directory / "local"))[0], 23)
        local.assert_called_once()
        self.assertEqual(self.api.calls, [])

    def test_configured_company_never_reads_stale_ledger(self):
        ledger = self.entity / "ledger.sqlite"
        ledger.write_bytes(b"not a database")
        self.assertEqual(self.command("queue", "summary")[0], 0)
        self.assertEqual(ledger.read_bytes(), b"not a database")
        self.assertEqual(self.api.calls[-1]["body"]["expected_books_revision"], 7)

    def test_insecure_broken_and_invalid_config_never_fall_back(self):
        original = self.config.read_text()
        with patch.object(cli.queue_module, "run", side_effect=AssertionError("local fallback")):
            self.config.chmod(0o644)
            self.assertEqual(self.command("queue", "summary")[0], 1)
            self.config.chmod(0o600)
            self.config.write_text("not json")
            self.assertEqual(self.command("queue", "summary")[0], 1)
            self.config.unlink()
            self.config.symlink_to(self.entity / "missing.json")
            self.assertEqual(self.command("queue", "summary")[0], 1)
            self.config.unlink()
            self.config.write_text(original)
        self.assertEqual(self.api.calls, [])

    def test_network_connectors_keep_local_credentials_and_outputs(self):
        with patch.object(Path, "cwd", return_value=self.entity), patch.object(cli, "run_connector", return_value=19) as runner, patch.dict(os.environ, {"STRIPE_SECRET_KEY": "synthetic-provider-key"}):
            code, _, _ = self.invoke("connector", "stripe", "download", "--output", str(self.directory / "stripe.json"))
        self.assertEqual(code, 19)
        runner.assert_called_once()
        self.assertEqual(self.api.calls, [])

    def test_network_connector_without_local_key_uses_company_credentials(self):
        for provider, leaf in (("banksync", "banks"), ("stripe", "account"), ("mercury", "accounts")):
            with self.subTest(provider=provider), patch.object(Path, "cwd", return_value=self.entity), patch.object(cli, "run_connector", side_effect=AssertionError("local fallback")):
                code, _, err = self.invoke("connector", provider, leaf)
            self.assertEqual(code, 0, err)
            self.assertEqual(self.api.calls[-1]["method"], "POST")
            self.assertEqual(self.api.calls[-1]["authorization"], "Bearer " + TOKEN)

    def test_custom_provider_key_selection_is_local_and_never_sent(self):
        with patch.object(Path, "cwd", return_value=self.entity), patch.object(cli, "run_connector", return_value=19) as runner, patch.dict(os.environ, {"SPECIAL_STRIPE_KEY": "private-provider-key"}):
            code, _, _ = self.invoke("connector", "stripe", "--api-key-env", "SPECIAL_STRIPE_KEY", "account")
        self.assertEqual(code, 19)
        runner.assert_called_once()
        self.assertEqual(self.api.calls, [])

    def test_missing_remote_provider_credential_does_not_fall_back(self):
        self.api.command_error = {"code": "PROVIDER_NOT_CONFIGURED", "message": "Company Stripe credential is not configured."}
        with patch.object(Path, "cwd", return_value=self.entity), patch.object(cli, "run_connector", side_effect=AssertionError("local fallback")):
            code, _, err = self.invoke("connector", "stripe", "account")
        self.assertEqual(code, 1)
        self.assertIn("PROVIDER_NOT_CONFIGURED", err)

    def test_local_provider_error_does_not_trigger_remote_fallback(self):
        with patch.object(Path, "cwd", return_value=self.entity), patch.object(cli, "run_connector", side_effect=cli.ProviderAPIError("Stripe", "Local key rejected")), patch.dict(os.environ, {"STRIPE_SECRET_KEY": "synthetic-key"}):
            self.assertEqual(self.invoke("connector", "stripe", "account")[0], 1)
        self.assertEqual(self.api.calls, [])

    def test_stale_intent_uses_observed_revision_until_explicit_read(self):
        self.assertEqual(self.command("queue", "list")[0], 0)
        self.api.revision = 8
        count = len(self.api.calls)
        code, _, err = self.command("queue", "confirm", "--item", "proposal-1")
        self.assertEqual(code, 1)
        self.assertIn("REVISION_CONFLICT", err)
        self.assertIn("review", err)
        self.assertEqual(len(self.api.calls), count + 1)
        self.assertEqual(self.api.calls[-1]["body"]["expected_books_revision"], 7)
        self.assertEqual(self.command("queue", "list")[0], 0)
        self.assertEqual(self.command("queue", "confirm", "--item", "proposal-1")[0], 0)
        self.assertEqual(self.api.calls[-1]["body"]["expected_books_revision"], 8)

    def test_returned_revision_including_artifact_changes_is_cached(self):
        self.api.result_revision = 9
        self.assertEqual(self.command("queue", "list")[0], 0)
        state = self.entity / ".slashbooks-remote-state.json"
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        self.assertEqual(json.loads(state.read_text())["last_seen_revision"], 9)
        before = len(self.api.calls)
        self.assertEqual(self.command("queue", "confirm", "--item", "proposal-1")[0], 0)
        self.assertEqual(len(self.api.calls), before + 1)
        self.assertEqual(self.api.calls[-1]["body"]["expected_books_revision"], 9)

    def test_failed_read_does_not_advance_intent_to_its_internal_status_probe(self):
        self.assertEqual(self.command("queue", "list")[0], 0)
        self.api.revision = 8
        self.api.command_error = {"code": "READ_FAILED"}
        self.assertEqual(self.command("queue", "list")[0], 1)
        state = json.loads((self.entity / ".slashbooks-remote-state.json").read_text())
        self.assertEqual(state["last_seen_revision"], 7)

    def test_reasoning_is_in_shared_command_explanation(self):
        code, _, err = self.command("queue", "propose", "--source-id", "source-1", "--category", "Expenses:Office", "--reasoning", "Matched the supplier invoice.")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.api.calls[-1]["body"]["explanation"], "Matched the supplier invoice.")

    def test_leading_dash_scalar_values_survive_engine_wire_rebuild(self):
        code, _, err = self.command("queue", "propose", "--source-id=-abc",
                                    "--category=Expenses:Office", "--reasoning=-word")
        self.assertEqual(code, 0, err)
        body = self.api.calls[-1]["body"]
        self.assertIn("--source-id=-abc", body["argv"])
        self.assertIn("--reasoning=-word", body["argv"])
        parsed, _, path = parse_command(body["argv"])
        self.assertEqual(path, ("queue", "propose"))
        self.assertEqual(parsed.source_id, "-abc")
        self.assertEqual(parsed.reasoning, "-word")
        self.assertEqual(body["explanation"], "-word")

    def test_uploads_input_bytes_and_freezes_request_before_send(self):
        source = self.directory / "source.json"
        source.write_bytes(b'{"transactions": []}')
        original = hosted.HostedClient.request

        def request(client, suffix, **kwargs):
            if suffix == "/engine/commands":
                records = self.receipts()
                self.assertEqual(len(records), 1)
                self.assertEqual(json.loads(records[0].read_text())["body"], kwargs["body"])
            return original(client, suffix, **kwargs)

        with patch.object(hosted.HostedClient, "request", request):
            self.assertEqual(self.command("ingest", str(source), "--source", "synthetic")[0], 0)
        body = self.api.calls[-1]["body"]
        self.assertEqual(body["inputs"], [{"path": "@input/input/0/source.json", "data_base64": base64.b64encode(source.read_bytes()).decode()}])
        self.assertIn("@input/input/0/source.json", body["argv"])
        self.assertEqual(stat.S_IMODE(self.receipts()[0].stat().st_mode), 0o600)

    def test_company_store_is_remote_even_with_stale_local_copy(self):
        store = self.entity / "ledger.sqlite"
        store.write_bytes(b"stale")
        self.api.artifacts = [artifact("@output/output/snapshot.json")]
        self.assertEqual(self.command("ledger", "snapshot", "--store", str(store), "--output", str(self.directory / "snapshot.json"))[0], 0)
        body = self.api.calls[-1]["body"]
        self.assertEqual(body["inputs"], [])
        self.assertIn("--store=@entity/ledger.sqlite", body["argv"])

    def test_migration_uploads_implicit_local_ledger_and_migrates_real_entries(self):
        source = self.entity / "books.beancount"
        source.write_bytes(LEGACY_LEDGER)
        self.api.command_handler = self._ledger_wire_handler
        code, out, err = self.command("ledger", "migrate")
        self.assertEqual(code, 0, err + out)
        self.assertIn("entries: 1", out)
        body = self.api.calls[-1]["body"]
        self.assertEqual([item["path"] for item in body["inputs"]], ["@entity/books.beancount"])
        self.assertEqual(base64.b64decode(body["inputs"][0]["data_base64"]), LEGACY_LEDGER)
        self.assertEqual(source.read_bytes(), LEGACY_LEDGER)
        self.assertFalse((self.entity / "ledger.sqlite").exists())

    def test_missing_migration_source_preserves_core_failure_not_zero_entry_success(self):
        self.api.command_handler = self._ledger_wire_handler
        code, out, _ = self.command("ledger", "migrate")
        self.assertEqual(code, 1)
        self.assertIn("Ledger file not found", out)
        self.assertNotIn("entries: 0", out)
        self.assertEqual(self.api.calls[-1]["body"]["inputs"], [])
        self.assertFalse((self.entity / "ledger.sqlite").exists())

    def test_migration_can_use_existing_remote_source_when_local_source_is_absent(self):
        server = self.directory / "synthetic-ledger-service"
        server.mkdir()
        (server / "books.beancount").write_bytes(LEGACY_LEDGER)
        self.api.command_handler = self._ledger_wire_handler
        code, out, err = self.command("ledger", "migrate")
        self.assertEqual(code, 0, err + out)
        self.assertIn("entries: 1", out)
        self.assertEqual(self.api.calls[-1]["body"]["inputs"], [])

    def test_migration_dry_run_uploads_source_without_requesting_store_download(self):
        source = self.entity / "books.beancount"
        source.write_bytes(LEGACY_LEDGER)
        destination = self.directory / "alt.sqlite"
        self.api.command_handler = self._ledger_wire_handler
        code, out, err = self.command("ledger", "migrate", "--dry-run", "--store", str(destination))
        self.assertEqual(code, 0, err + out)
        self.assertIn("entries: 1", out)
        self.assertEqual(self.api.calls[-1]["body"]["inputs"][0]["path"], "@entity/books.beancount")
        self.assertFalse(destination.exists())
        self.assertFalse((self.entity / "ledger.sqlite").exists())
        self.assertFalse((self.directory / "synthetic-ledger-service/alternate.sqlite").exists())
        self.assertEqual(source.read_bytes(), LEGACY_LEDGER)

    def test_external_store_migration_download_then_snapshot_upload_is_lossless(self):
        (self.entity / "books.beancount").write_bytes(LEGACY_LEDGER)
        store = self.directory / "alt.sqlite"
        snapshot = self.directory / "snapshot.beancount"
        self.api.command_handler = self._ledger_wire_handler
        code, out, err = self.command("ledger", "migrate", "--store", str(store))
        self.assertEqual(code, 0, err + out)
        self.assertIn("--store=@output/store/alt.sqlite", self.api.calls[-1]["body"]["argv"])
        store_bytes = store.read_bytes()
        self.assertEqual(store_bytes, (self.directory / "synthetic-ledger-service/alternate.sqlite").read_bytes())
        self.assertTrue(store_bytes.startswith(b"SQLite format 3\x00"))
        self.assertEqual([entry.source_id for entry in LedgerStore(store).load_entries()], ["legacy-parity-1"])
        code, out, err = self.command("ledger", "snapshot", "--store", str(store), "--output", str(snapshot))
        self.assertEqual(code, 0, err + out)
        body = self.api.calls[-1]["body"]
        self.assertIn("--store=@input/store/0/alt.sqlite", body["argv"])
        self.assertEqual(body["inputs"][0]["path"], "@input/store/0/alt.sqlite")
        self.assertEqual(base64.b64decode(body["inputs"][0]["data_base64"]), store_bytes)
        self.assertEqual(snapshot.read_bytes(), render_store_ledger(store).encode())
        self.assertFalse((self.entity / "ledger.sqlite").exists())

    def test_implicit_migration_source_symlinks_fail_before_submission(self):
        source = self.directory / "source.beancount"
        source.write_bytes(LEGACY_LEDGER)
        (self.entity / "books.beancount").symlink_to(source)
        self.assertEqual(self.command("ledger", "migrate")[0], 1)
        self.assertEqual(self.api.calls, [])

    def test_unreadable_implicit_migration_source_does_not_use_remote_fallback(self):
        source = self.entity / "books.beancount"
        source.write_bytes(LEGACY_LEDGER)
        original = Path.lstat

        def inaccessible(path, *args, **kwargs):
            if path == source:
                raise PermissionError("Synthetic source permissions")
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", inaccessible):
            self.assertEqual(self.command("ledger", "migrate")[0], 1)
        self.assertEqual(self.api.calls, [])

    def test_quarterly_review_downloads_exact_core_report_files(self):
        json_data = b'{"quarter": 3, "year": 2026}\n'
        text_data = b"Quarterly Review: Q3 2026\nSynthetic report\n"
        self.api.artifacts = [artifact("@entity/reports/quarterly/2026-Q3.json", json_data),
                              artifact("@entity/reports/quarterly/2026-Q3.txt", text_data),
                              artifact("@entity/reports/quarterly/2026-Q2.txt", b"unrequested"),
                              artifact("@entity/reports/unrequested.json", b"unrequested")]
        for _ in range(2):
            code, _, err = self.command("quarterly-review", "--quarter", "Q3", "--year", "2026")
            self.assertEqual(code, 0, err)
            self.assertEqual((self.entity / "reports/quarterly/2026-Q3.json").read_bytes(), json_data)
            self.assertEqual((self.entity / "reports/quarterly/2026-Q3.txt").read_bytes(), text_data)
        self.assertFalse((self.entity / "reports/quarterly/2026-Q2.txt").exists())
        self.assertFalse((self.entity / "reports/unrequested.json").exists())

    def test_quarterly_review_missing_report_does_not_claim_download_success(self):
        self.api.artifacts = [artifact("@entity/reports/quarterly/2026-Q3.json", b"{}\n")]
        code, _, err = self.command("quarterly-review", "--quarter", "Q3", "--year", "2026")
        self.assertEqual(code, 1)
        self.assertIn("MISSING_ARTIFACT", err)
        self.assertFalse((self.entity / "reports/quarterly/2026-Q3.json").exists())

    def test_requested_company_root_snapshot_and_normalized_json_are_downloaded(self):
        snapshot = self.entity / "snapshot.beancount"
        self.api.artifacts = [artifact("@entity/snapshot.beancount", LEGACY_LEDGER)]
        self.assertEqual(self.command("ledger", "snapshot", "--output", str(snapshot))[0], 0)
        self.assertIn("--output=@entity/snapshot.beancount", self.api.calls[-1]["body"]["argv"])
        self.assertEqual(snapshot.read_bytes(), LEGACY_LEDGER)
        source = self.directory / "bank.csv"
        source.write_bytes(b"Date,Amount\n2026-01-01,19.00\n")
        normalized = self.entity / "normalized.json"
        normalized_data = b'[{"amount":"19.00"}]\n'
        self.api.artifacts = [artifact("@entity/normalized.json", normalized_data)]
        self.assertEqual(self.command("connector", "csv", "parse", str(source), "--output", str(normalized))[0], 0)
        self.assertIn("--output=@entity/normalized.json", self.api.calls[-1]["body"]["argv"])
        self.assertEqual(normalized.read_bytes(), normalized_data)

    def test_csv_json_output_reports_downloaded_path_and_preserves_descriptions(self):
        source = self.directory / "bank.csv"
        source.write_bytes(b"Date,Amount\n2026-01-01,19.00\n")
        output = self.entity / "normalized.json"
        wire = "@entity/normalized.json"
        data = b'[{"id":"synthetic-1","amount":"19.00"}]\n'
        document = {"output": wire, "description": wire,
                    "transactions": [{"description": "Invoice mentions @entity/normalized.json"}],
                    "input": "@input/file/0/bank.csv", "ledger_path": "@entity/ledger.sqlite"}
        self.api.stdout = json.dumps(document) + "\n"
        original_stdout = self.api.stdout
        self.api.artifacts = [artifact(wire, data)]
        code, out, err = self.command("connector", "csv", "parse", str(source), "--output", str(output))
        self.assertEqual(code, 0, err)
        parsed = json.loads(out)
        self.assertEqual(parsed, {**document, "output": str(output)})
        self.assertEqual(Path(parsed["output"]).read_bytes(), data)
        receipt = self.receipts()[0]
        self.assertEqual(json.loads(receipt.read_text())["response"]["stdout"], original_stdout)
        calls = len(self.api.calls)
        retry_code, retry_out, retry_err = self.invoke("hosted", "retry", "--receipt", str(receipt))
        self.assertEqual(retry_code, 0, retry_err)
        self.assertEqual(retry_out, out)
        self.assertEqual(len(self.api.calls), calls)
        self.api.artifacts = []
        self.api.stdout = "ingested\n"
        code, _, err = self.command("ingest", parsed["output"], "--source", "csv")
        self.assertEqual(code, 0, err)
        self.assertEqual(base64.b64decode(self.api.calls[-1]["body"]["inputs"][0]["data_base64"]), data)

    def test_external_output_directory_json_maps_only_downloaded_files_and_directories(self):
        output = self.directory / "Accountant Export"
        wire = "@output/output_dir/Accountant Export"
        document = {"output_dir": wire, "csv_dir": wire + "/csv", "files": [wire + "/csv/pnl.csv", wire + "/missing.csv"],
                    "nested": {"path": wire + "/csv/pnl.csv", "description": wire + "/csv/pnl.csv"},
                    "ledger_path": "@entity/ledger.sqlite", "path": "@entity/input.json"}
        self.api.stdout = json.dumps(document, indent=2) + "\n"
        self.api.artifacts = [artifact(wire + "/csv/pnl.csv", b"account,amount\nIncome,19.00\n"),
                              artifact("@entity/ledger.sqlite", b"unrequested state")]
        code, out, err = self.command("export", "--from", "2026-01-01", "--to", "2026-03-31", "--output-dir", str(output))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["output_dir"], str(output))
        self.assertEqual(result["csv_dir"], str(output / "csv"))
        self.assertEqual(result["files"], [str(output / "csv/pnl.csv"), wire + "/missing.csv"])
        self.assertEqual(result["nested"], {"path": str(output / "csv/pnl.csv"), "description": wire + "/csv/pnl.csv"})
        self.assertEqual(result["ledger_path"], document["ledger_path"])
        self.assertEqual(result["path"], document["path"])
        self.assertTrue(Path(result["files"][0]).is_file())
        self.assertFalse((self.entity / "ledger.sqlite").exists())

    def test_plaintext_artifact_messages_use_exact_paths_not_business_description_substrings(self):
        output = self.entity / "snapshot.beancount"
        wire = "@entity/snapshot.beancount"
        self.api.artifacts = [artifact(wire, LEGACY_LEDGER)]
        untouched = (f"Description: mentions {wire}\n"
                     f"Snapshot written: {wire}.backup\n"
                     "Migrated: @entity/ledger.sqlite\n"
                     "File: @input/source.beancount\n")
        self.api.stdout = f"Snapshot written: {wire}\n" + untouched
        self.api.stderr = f"Written to {wire}\n"
        code, out, err = self.command("ledger", "snapshot", "--output", str(output))
        self.assertEqual(code, 0, err)
        self.assertEqual(out, f"Snapshot written: {output}\n" + untouched)
        self.assertTrue(err.endswith(f"Written to {output}\n"))

    def test_plaintext_external_export_paths_with_spaces_are_usable(self):
        output = self.directory / "Accountant Export"
        wire = "@output/output_dir/Accountant Export"
        self.api.artifacts = [artifact(wire + "/csv/pnl.csv", b"csv"), artifact(wire + "/workbook.xlsx", b"xlsx")]
        self.api.stdout = (f"Accountant export written to: {wire}\n"
                           f"  CSV exports: 1 files in {wire}/csv\n"
                           f"  XLSX workbook: {wire}/workbook.xlsx\n")
        code, out, err = self.command("export", "--from", "2026-01-01", "--to", "2026-03-31", "--output-dir", str(output))
        self.assertEqual(code, 0, err)
        self.assertEqual(out, (f"Accountant export written to: {output}\n"
                               f"  CSV exports: 1 files in {output / 'csv'}\n"
                               f"  XLSX workbook: {output / 'workbook.xlsx'}\n"))

    def test_unmaterialized_output_directory_is_not_reported_as_local(self):
        output = self.directory / "empty-export"
        wire = "@output/output_dir/empty-export"
        self.api.stdout = json.dumps({"output_dir": wire, "output": wire + "/missing.csv"}) + "\n"
        self.api.artifacts = []
        code, out, err = self.command("export", "--from", "2026-01-01", "--to", "2026-03-31", "--output-dir", str(output))
        self.assertEqual(code, 0, err)
        self.assertEqual(out, self.api.stdout)
        self.assertFalse(output.exists())

    def test_folder_and_multiple_file_uploads(self):
        folder = self.directory / "qb"
        (folder / "nested").mkdir(parents=True)
        (folder / "nested/report.csv").write_bytes(b"synthetic")
        sources = [self.directory / "one.json", self.directory / "two.json"]
        for source in sources:
            source.write_text("{}")
        code, _, err = self.command("backtest", "run", "--qb-folder", str(folder), "--from", "2026-01-01", "--to", "2026-03-31",
                                    "--banksync-json", *(str(p) for p in sources), "--skip-fetch")
        self.assertEqual(code, 0, err)
        inputs = self.api.calls[-1]["body"]["inputs"]
        self.assertEqual(len(inputs), 3)
        self.assertIn("@input/qb_folder/0/qb/nested/report.csv", [f["path"] for f in inputs])

    def test_missing_company_input_is_a_remote_reference(self):
        source = self.entity / "cache/source.json"
        self.assertEqual(self.command("ingest", str(source), "--source", "synthetic")[0], 0)
        body = self.api.calls[-1]["body"]
        self.assertEqual(body["inputs"], [])
        self.assertIn("@entity/cache/source.json", body["argv"])

    def test_downloads_only_mapped_output_and_preserves_streams(self):
        output = self.directory / "pnl.json"
        self.api.artifacts = [artifact("@output/output/pnl.json"), artifact("@entity/ledger.sqlite", b"do not mirror")]
        self.api.stdout, self.api.stderr = "exact stdout", "exact stderr"
        code, out, err = self.command("report", "pnl", "--from", "2026-01-01", "--to", "2026-03-31", "--output", str(output))
        self.assertEqual((code, out), (0, "exact stdout"))
        self.assertTrue(err.endswith("exact stderr"))
        self.assertEqual(output.read_bytes(), b"synthetic output\n")
        self.assertFalse((self.entity / "ledger.sqlite").exists())

    def test_export_default_downloads_only_export_directory(self):
        self.api.artifacts = [artifact("@entity/reports/accountant-export/period/report.xlsx", b"xlsx"), artifact("@entity/cache/raw.json")]
        self.assertEqual(self.command("export", "--from", "2026-01-01", "--to", "2026-03-31")[0], 0)
        self.assertEqual((self.entity / "reports/accountant-export/period/report.xlsx").read_bytes(), b"xlsx")
        self.assertFalse((self.entity / "cache/raw.json").exists())

    def test_traversal_and_bad_checksums_fail_before_writes(self):
        output = self.directory / "out.json"
        for value in [artifact("@output/output/../escape"), {**artifact("@output/output/out.json"), "sha256": "wrong"}]:
            self.api.artifacts = [value]
            self.assertEqual(self.command("report", "trial-balance", "--as-of", "2026-03-31", "--output", str(output))[0], 1)
            self.assertFalse(output.exists())

    def test_nonzero_exit_is_not_a_transport_error(self):
        self.api.exit_code = 3
        self.api.stderr = "command validation failed\n"
        code, _, err = self.command("queue", "summary")
        self.assertEqual(code, 3)
        self.assertTrue(err.endswith(self.api.stderr))

    def test_uncertain_retry_uses_same_body_key_and_original_input(self):
        source = self.directory / "input.json"
        source.write_text("{}")
        self.api.drop = True
        self.assertEqual(self.command("ingest", str(source), "--source", "synthetic")[0], 1)
        original = self.api.calls[-1]
        source.write_text("changed")
        self.api.drop = False
        code, _, err = self.invoke("hosted", "retry", "--receipt", str(self.receipts()[0]))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.api.calls[-1]["key"], original["key"])
        self.assertEqual(self.api.calls[-1]["body"], original["body"])
        self.assertEqual(len(self.api.results), 1)

    def test_download_failure_recovers_without_another_request(self):
        output = self.directory / "report.json"
        self.api.artifacts = [artifact("@output/output/report.json")]
        with patch.object(remote, "_publish", side_effect=OSError("synthetic disk error")):
            code, _, err = self.command("report", "trial-balance", "--as-of", "2026-03-31", "--output", str(output))
        self.assertEqual(code, 1)
        self.assertIn("local_receipt", err)
        calls = len(self.api.calls)
        self.assertEqual(self.invoke("hosted", "retry", "--receipt", str(self.receipts()[0]))[0], 0)
        self.assertEqual(len(self.api.calls), calls)
        self.assertTrue(output.exists())

    def test_changed_receipt_or_company_cannot_retry(self):
        self.command("queue", "summary")
        receipt = self.receipts()[0]
        original = receipt.read_text()
        value = json.loads(original)
        value["body"]["argv"] = ["queue", "confirm"]
        receipt.write_text(json.dumps(value))
        before = len(self.api.calls)
        self.assertEqual(self.invoke("hosted", "retry", "--receipt", str(receipt))[0], 1)
        receipt.write_text(original)
        config = json.loads(self.config.read_text())
        config["company"] = "company-B"
        self.config.write_text(json.dumps(config))
        self.assertEqual(self.invoke("hosted", "retry", "--receipt", str(receipt))[0], 1)
        self.assertEqual(len(self.api.calls), before)

    def test_file_list_get_put_json_and_stale_revision(self):
        local = self.directory / "notes.md"
        code, out, _ = self.invoke("hosted", "file", "list", "--entity", str(self.entity))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["books_revision"], 7)
        code, out, _ = self.invoke("hosted", "file", "get", "context/notes.md", "--output", str(local), "--entity", str(self.entity))
        self.assertEqual(code, 0)
        self.assertEqual(local.read_bytes(), self.api.file)
        self.assertEqual(json.loads(out)["books_revision"], 7)
        local.write_text("edited")
        self.api.revision = 8
        code, _, err = self.invoke("hosted", "file", "put", "context/notes.md", "--file", str(local), "--entity", str(self.entity))
        self.assertEqual(code, 1)
        self.assertIn("409", err)
        self.assertEqual(self.api.calls[-1]["body"]["expected_books_revision"], 7)
        self.assertEqual(local.read_text(), "edited")

    def test_new_file_put_and_retry_use_put_and_saved_response(self):
        local = self.directory / "new.md"
        local.write_text("new notes")
        with patch.object(Path, "cwd", return_value=self.entity):
            code, out, err = self.invoke("hosted", "file", "put", "context/new.md", "--file", str(local))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["books_revision"], 8)
        self.assertEqual(self.api.calls[-1]["method"], "PUT")
        calls = len(self.api.calls)
        self.assertEqual(self.invoke("hosted", "retry", "--receipt", str(self.receipts()[0]))[0], 0)
        self.assertEqual(len(self.api.calls), calls)

    def test_onboarding_checkpoint_survives_lost_response_and_fresh_download(self):
        local = self.directory / "checkpoint.md"
        checkpoint = "Confirmed: synthetic consulting.\nOpen: opening balance evidence.\nNext: import agreed period.\n"
        local.write_text(checkpoint)
        self.api.drop = True
        code, _, _ = self.invoke("hosted", "file", "put", "ONBOARDING.md", "--file", str(local), "--entity", str(self.entity))
        self.assertEqual(code, 1)
        self.assertEqual(self.api.revision, 8)
        self.api.drop = False
        self.assertEqual(self.invoke("hosted", "retry", "--receipt", str(self.receipts()[0]))[0], 0)
        self.assertEqual(self.api.revision, 8)
        local.unlink()
        downloaded = self.directory / "fresh-checkpoint.md"
        code, _, err = self.invoke("hosted", "file", "get", "ONBOARDING.md", "--output", str(downloaded), "--entity", str(self.entity))
        self.assertEqual(code, 0, err)
        self.assertEqual(downloaded.read_text(), checkpoint)

    def test_file_path_traversal_is_rejected(self):
        code, _, _ = self.invoke("hosted", "file", "get", "../notes.md", "--output", str(self.directory / "out"), "--entity", str(self.entity))
        self.assertEqual(code, 1)
        self.assertEqual(self.api.calls, [])

    def test_existing_file_without_baseline_requires_explicit_intent(self):
        local = self.directory / "notes.md"
        local.write_text("possibly stale")
        code, _, err = self.invoke("hosted", "file", "put", "context/notes.md", "--file", str(local), "--entity", str(self.entity))
        self.assertEqual(code, 1)
        self.assertIn("BASELINE_REQUIRED", err)
        self.assertTrue(all(call["method"] == "GET" for call in self.api.calls))
        code, out, err = self.invoke("hosted", "file", "put", "context/notes.md", "--file", str(local), "--entity", str(self.entity), "--overwrite")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["books_revision"], 8)

    def test_successful_put_advances_baseline_and_old_receipt_cannot_regress_it(self):
        local = self.directory / "notes.md"
        self.assertEqual(self.invoke("hosted", "file", "get", "context/notes.md", "--output", str(local), "--entity", str(self.entity))[0], 0)
        local.write_text("first edit")
        self.assertEqual(self.invoke("hosted", "file", "put", "context/notes.md", "--file", str(local), "--entity", str(self.entity))[0], 0)
        old_receipt = self.receipts()[0]
        local.write_text("second edit")
        self.assertEqual(self.invoke("hosted", "file", "put", "context/notes.md", "--file", str(local), "--entity", str(self.entity))[0], 0)
        self.assertEqual(self.api.calls[-1]["body"]["expected_books_revision"], 8)
        calls = len(self.api.calls)
        self.assertEqual(self.invoke("hosted", "retry", "--receipt", str(old_receipt))[0], 0)
        self.assertEqual(len(self.api.calls), calls)
        local.write_text("third edit")
        self.assertEqual(self.invoke("hosted", "file", "put", "context/notes.md", "--file", str(local), "--entity", str(self.entity))[0], 0)
        self.assertEqual(self.api.calls[-1]["body"]["expected_books_revision"], 9)
        metadata = list((self.entity / ".slashbooks-remote-downloads").glob("*.json"))
        self.assertEqual(len(metadata), 1)
        self.assertEqual(stat.S_IMODE(metadata[0].stat().st_mode), 0o600)
        self.assertEqual(json.loads(metadata[0].read_text())["sha256"], hashlib.sha256(b"third edit").hexdigest())

    def test_auth_and_http_failure_never_call_local_engine(self):
        with patch.object(cli.queue_module, "run", side_effect=AssertionError("local fallback")):
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(self.command("queue", "summary")[0], 1)
            self.assertEqual(self.api.calls, [])
            self.api.status = 403
            self.assertEqual(self.command("queue", "summary")[0], 1)
        self.assertTrue(all(call["method"] == "GET" for call in self.api.calls))

    def test_invalid_revision_and_missing_output_are_not_success(self):
        self.api.revision = True
        self.assertEqual(self.command("queue", "summary")[0], 1)
        self.assertTrue(all(call["method"] == "GET" for call in self.api.calls))
        self.api.revision = 7
        code, out, err = self.command("report", "trial-balance", "--as-of", "2026-03-31", "--output", str(self.directory / "absent.json"))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("MISSING_ARTIFACT", err)

    def test_original_input_token_is_not_saved_in_receipts(self):
        source = self.directory / "input.json"
        source.write_text(json.dumps({"text": TOKEN}))
        code, _, err = self.command("ingest", str(source), "--source", "synthetic")
        self.assertEqual(code, 1)
        self.assertIn("SECRET_IN_INPUT", err)
        self.assertEqual(self.receipts(), [])

    def test_directory_upload_skips_credential_files_and_receipts(self):
        folder = self.directory / "exports"
        folder.mkdir()
        (folder / "report.csv").write_text("synthetic")
        (folder / ".env").write_text(TOKEN)
        (folder / ".env.local").write_text(TOKEN)
        (folder / remote.CONFIG_NAME).write_text(TOKEN)
        receipts = folder / ".slashbooks-remote-receipts"
        receipts.mkdir()
        (receipts / "receipt.json").write_text(TOKEN)
        with patch.object(Path, "cwd", return_value=self.entity):
            code, _, err = self.invoke("qb", "inventory", str(folder))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.api.calls[-1]["body"]["inputs"]), 1)
        self.assertTrue(self.api.calls[-1]["body"]["inputs"][0]["path"].endswith("/report.csv"))

    def test_exact_12_mib_input_is_rejected_for_encoded_wire_overhead(self):
        source = self.directory / "large.json"
        source.write_bytes(b"a" * (12 * 1024 * 1024))
        code, _, err = self.command("ingest", str(source), "--source", "synthetic")
        self.assertEqual(code, 1)
        self.assertIn("REQUEST_TOO_LARGE", err)
        self.assertNotIn("uncertain", err)
        self.assertTrue(all(call["method"] == "GET" for call in self.api.calls))
        self.assertEqual(self.receipts(), [])

    def test_download_does_not_overwrite_a_local_edit_after_submission(self):
        output = self.directory / "out.json"
        output.write_text("original")
        self.api.artifacts = [artifact("@output/output/out.json")]
        original = hosted.HostedClient.request

        def request(client, suffix, **kwargs):
            result = original(client, suffix, **kwargs)
            if suffix == "/engine/commands":
                output.write_text("concurrent edit")
            return result

        with patch.object(hosted.HostedClient, "request", request):
            code, _, err = self.command("report", "trial-balance", "--as-of", "2026-03-31", "--output", str(output))
        self.assertEqual(code, 1)
        self.assertIn("OUTPUT_CHANGED", err)
        self.assertEqual(output.read_text(), "concurrent edit")

    def test_symlink_input_and_output_are_rejected_before_network(self):
        real = self.directory / "real"
        real.write_text("synthetic")
        link = self.directory / "link"
        link.symlink_to(real)
        self.assertEqual(self.command("ingest", str(link), "--source", "synthetic")[0], 1)
        self.assertEqual(self.command("report", "trial-balance", "--as-of", "2026-03-31", "--output", str(link))[0], 1)
        self.assertEqual(self.api.calls, [])


if __name__ == "__main__":
    unittest.main()
