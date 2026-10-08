from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bookkeeping import __version__, cli, hosted  # noqa: E402


TOKEN = "synthetic-hosted-token-never-print"


class LocalAPI:
    """Wire-level fixture only; not a substitute for real service authorization tests."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.response = {"ok": True}
        self.status = 200
        self.location = ""
        self.raw: bytes | None = None
        self.delay = 0.0
        self.body_delay = 0.0
        self.drop = False
        self.on_request = None
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.respond()

            def do_POST(self) -> None:
                self.respond()

            def log_message(self, *args: object) -> None:
                pass

            def respond(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fixture.calls.append({"method": self.command, "path": self.path,
                                      "headers": dict(self.headers), "body": json.loads(body) if body else None})
                if fixture.on_request:
                    fixture.on_request()
                if fixture.drop:
                    self.close_connection = True
                    return
                delay = fixture.delay
                status = fixture.status
                raw = fixture.raw if fixture.raw is not None else json.dumps(fixture.response).encode()
                location = fixture.location
                time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    if location:
                        self.send_header("Location", location)
                    self.end_headers()
                    time.sleep(fixture.body_delay)
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.endpoint = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class HostedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.config = self.directory / "hosted.json"
        self.api = LocalAPI()
        self.addCleanup(self.api.close)
        self.env = patch.dict(os.environ, {"BOOKS_API_TOKEN": TOKEN}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.dotenv = patch.object(cli, "load_dotenv")
        self.dotenv.start()
        self.addCleanup(self.dotenv.stop)
        code, _, _ = self.invoke("configure", "--endpoint", self.api.endpoint, "--company", "company-A", "--allow-localhost")
        self.assertEqual(code, 0)

    def invoke(self, *args: str, timeout: str = "2") -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["hosted", "--config", str(self.config), "--timeout", timeout, *args])
        stdout, stderr = out.getvalue(), err.getvalue()
        self.assertNotIn(TOKEN, stdout + stderr)
        if stdout:
            json.loads(stdout)
        for line in stderr.splitlines():
            json.loads(line)
        return code, stdout, stderr

    def file(self, body: dict, name: str = "input.json") -> str:
        path = self.directory / name
        path.write_text(json.dumps(body), encoding="utf-8")
        return str(path)

    def body(self, operation: str = "run.start") -> dict:
        body = {"operation": operation, "payload": {"purpose": "Synthetic review"}}
        if operation.startswith("queue."):
            body["expected_books_revision"] = 7
            body["payload"] = {"proposal_id": "proposal-1", "version": 2}
        if operation == "queue.propose":
            body["payload"] = {"date": "2026-09-01", "description": "Synthetic item",
                               "postings": [{"account": "Assets:Cash", "amount": "2.00", "currency": "USD"},
                                            {"account": "Income:Sales", "amount": "-2.00", "currency": "USD"}]}
            body["explanation"] = {"summary": "Matched source", "evidence_ids": ["ev-1"],
                                   "policy_versions": ["policy-2"], "assumptions": [], "open_questions": []}
        return body

    def receipts(self) -> list[Path]:
        return list((self.directory / "hosted-receipts").glob("*.json"))

    def test_config_does_not_store_token_unless_requested(self) -> None:
        config = json.loads(self.config.read_text())
        self.assertNotIn("token", config)
        self.assertEqual(config["endpoint"], self.api.endpoint + "/api/v1")
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(self.api.calls, [])

    def test_integrations_reads_the_company_connection_summary_without_mutating(self) -> None:
        self.api.response = {"bankConnectionVerified": True,
                             "bankConnections": [{"name": "Mercury", "accounts": []}]}
        code, out, err = self.invoke("integrations")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(json.loads(out)["bankConnectionVerified"])
        self.assertEqual(self.api.calls[-1]["method"], "GET")
        self.assertEqual(self.api.calls[-1]["path"], "/api/v1/companies/company-A/integrations")
        self.assertEqual(len(self.api.calls), 1)

    def test_private_token_config_and_environment_precedence(self) -> None:
        code, _, _ = self.invoke("configure", "--endpoint", self.api.endpoint, "--company", "company-A", "--allow-localhost", "--store-token")
        self.assertEqual(code, 0)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.invoke("status")[0], 0)
        self.assertEqual(self.api.calls[-1]["headers"]["Authorization"], "Bearer " + TOKEN)
        with patch.dict(os.environ, {"BOOKS_API_TOKEN": "synthetic-override"}):
            self.api.response = {"echo": TOKEN + " synthetic-override", "token": "another-secret"}
            code, out, _ = self.invoke("status")
        self.assertEqual(code, 0)
        self.assertNotIn("synthetic-override", out)
        self.assertNotIn("another-secret", out)
        self.assertEqual(self.api.calls[-1]["headers"]["Authorization"], "Bearer synthetic-override")

    def test_insecure_token_config_rejected_before_network(self) -> None:
        config = json.loads(self.config.read_text())
        config["token"] = TOKEN
        self.config.write_text(json.dumps(config))
        self.config.chmod(0o644)
        code, _, err = self.invoke("status")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err)["error"]["code"], "INSECURE_FILE")
        self.assertFalse(self.api.calls)

    def test_missing_token_is_nonzero_without_local_fallback(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            code, out, err = self.invoke("status")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err)["error"]["code"], "AUTH_REQUIRED")
        self.assertFalse(self.api.calls)
        self.assertFalse((self.directory / "ledger.sqlite").exists())

    def test_all_reads_are_company_scoped(self) -> None:
        for command, suffix in hosted.READS.items():
            with self.subTest(command=command):
                self.assertEqual(self.invoke(command)[0], 0)
                call = self.api.calls[-1]
                self.assertEqual(call["path"], "/api/v1/companies/company-A" + suffix)
                self.assertEqual(call["method"], "GET")
                self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
                self.assertEqual(call["headers"]["Accept"], "application/json")

    def test_requests_identify_slashbooks_version(self) -> None:
        client = hosted.HostedClient(json.loads(self.config.read_text()))
        for method, body in (("GET", None), ("POST", self.body())):
            with self.subTest(method=method):
                client.request("", method=method, body=body, key="synthetic-user-agent-test")
                call = self.api.calls[-1]
                self.assertEqual(call["method"], method)
                self.assertEqual(call["headers"]["User-Agent"], f"slashbooks/{__version__}")

    def test_report_query_and_command_id_encoding(self) -> None:
        self.assertEqual(self.invoke("reports", "--from", "2026-09-01", "--to", "2026-09-19&company=other")[0], 0)
        parsed = urlsplit(self.api.calls[-1]["path"])
        self.assertEqual(parse_qs(parsed.query), {"from": ["2026-09-01"], "to": ["2026-09-19&company=other"]})
        self.assertEqual(self.invoke("command-result", "command?other#fragment")[0], 0)
        self.assertEqual(self.api.calls[-1]["path"], "/api/v1/companies/company-A/commands/command%3Fother%23fragment")

    def test_collection_query_encoding_keeps_cursor_opaque(self) -> None:
        cursor = "https://untrusted.example/a?company=B&cursor=next#x +/=\u00e9"
        for command in hosted.COLLECTIONS:
            with self.subTest(command=command):
                args = [command, "--cursor", cursor, "--limit", "37"]
                expected = {"cursor": [cursor], "limit": ["37"]}
                if command in hosted.STATUS_READS:
                    args.extend(["--status", "waiting_for_review"])
                    expected["status"] = ["waiting_for_review"]
                self.assertEqual(self.invoke(*args)[0], 0)
                call = self.api.calls[-1]
                parsed = urlsplit(call["path"])
                self.assertEqual(parsed.path, "/api/v1/companies/company-A/" + command)
                self.assertEqual(parse_qs(parsed.query), expected)
                self.assertEqual(call["method"], "GET")
                self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)

    def test_all_pages_collects_over_200_records_and_preserves_filters(self) -> None:
        rows = [{"id": str(n)} for n in range(451)]

        def respond() -> None:
            cursor = parse_qs(urlsplit(self.api.calls[-1]["path"]).query).get("cursor", [None])[0]
            start = {None: 0, "opaque+/=&one": 200, "opaque+/=&two": 400}[cursor]
            self.api.response = {"sources": rows[start:start + 200],
                                 "next_cursor": {0: "opaque+/=&one", 200: "opaque+/=&two", 400: None}[start]}

        self.api.on_request = respond
        code, out, err = self.invoke("sources", "--all", "--status", "staged", "--limit", "200")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"sources": rows, "next_cursor": None, "pages_read": 3})
        for call in self.api.calls:
            query = parse_qs(urlsplit(call["path"]).query)
            self.assertEqual(query["status"], ["staged"])
            self.assertEqual(query["limit"], ["200"])
        self.assertEqual(len(self.api.calls), 3)

    def test_all_pages_collection_keys_and_empty_terminal_page(self) -> None:
        for command, collection in hosted.COLLECTIONS.items():
            with self.subTest(command=command):
                self.api.response = {collection: [], "next_cursor": None}
                code, out, _ = self.invoke(command, "--all", "--cursor", "start-here")
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out), {collection: [], "next_cursor": None, "pages_read": 1})
                query = parse_qs(urlsplit(self.api.calls[-1]["path"]).query)
                self.assertEqual(query, {"limit": ["200"], "cursor": ["start-here"]})

    def test_empty_intermediate_page_does_not_end_scan(self) -> None:
        def respond() -> None:
            if len(self.api.calls) == 1:
                self.api.response = {"sources": [], "next_cursor": "still-more"}
            else:
                self.api.response = {"sources": [{"id": "last-source"}], "next_cursor": None}

        self.api.on_request = respond
        code, out, _ = self.invoke("sources", "--all", "--max-pages", "2")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"sources": [{"id": "last-source"}], "next_cursor": None, "pages_read": 2})
        self.assertEqual(len(self.api.calls), 2)

    def test_single_page_returns_cursor_without_following_it(self) -> None:
        self.api.response = {"proposals": [{"id": "p-1"}], "next_cursor": "opaque-next"}
        code, out, _ = self.invoke("proposals", "--status", "open", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), self.api.response)
        self.assertEqual(len(self.api.calls), 1)

    def test_all_pages_repeated_and_cyclic_cursors_stop_without_partial_output(self) -> None:
        for sequence, initial, expected_calls in ((["a", "a"], None, 2), (["a", "b", "a"], None, 3), (["a"], "a", 1)):
            with self.subTest(sequence=sequence, initial=initial):
                self.api.calls.clear()

                def respond() -> None:
                    self.api.response = {"events": [], "next_cursor": sequence[len(self.api.calls) - 1]}

                self.api.on_request = respond
                args = ["history", "--all"]
                if initial:
                    args.extend(["--cursor", initial])
                code, out, err = self.invoke(*args)
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(json.loads(err)["error"]["code"], "PAGINATION_LOOP")
                self.assertEqual(len(self.api.calls), expected_calls)

    def test_all_pages_stops_at_bounded_page_count(self) -> None:
        def respond() -> None:
            self.api.response = {"entries": [{"id": str(len(self.api.calls))}], "next_cursor": str(len(self.api.calls))}

        self.api.on_request = respond
        code, out, err = self.invoke("entries", "--all", "--max-pages", "2")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err)["error"]["code"], "PAGINATION_LIMIT")
        self.assertEqual(len(self.api.calls), 2)

    def test_all_pages_fails_on_missing_or_invalid_pagination_metadata(self) -> None:
        for response in ({"sources": []}, {"sources": {}, "next_cursor": None},
                         {"sources": [], "next_cursor": ""}, {"sources": [], "next_cursor": 1},
                         {"sources": [], "next_cursor": ["a"]}):
            self.api.response = response
            code, out, _ = self.invoke("sources", "--all")
            self.assertEqual((code, out), (1, ""))
        self.assertEqual(len(self.api.calls), 5)

    def test_pagination_options_are_bounded_before_network(self) -> None:
        for args in (("--limit", "0"), ("--limit", "201"), ("--limit", "-1"),
                     ("--all", "--max-pages", "0"), ("--all", "--max-pages", "101"),
                     ("--max-pages", "2"), ("--cursor", ""), ("--cursor", "x" * 8193),
                     ("--status", "open&company=B")):
            self.assertEqual(self.invoke("sources", *args)[0], 1)
        self.assertFalse(self.api.calls)

    def test_cursor_and_status_never_expose_or_forward_api_credentials(self) -> None:
        self.assertEqual(self.invoke("sources", "--cursor", TOKEN)[0], 1)
        self.assertEqual(self.invoke("sources", "--status", TOKEN)[0], 1)
        self.assertFalse(self.api.calls)
        self.api.response = {"sources": [], "next_cursor": "echo-" + TOKEN}
        for args in (("sources",), ("sources", "--all")):
            code, out, err = self.invoke(*args)
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(json.loads(err)["error"]["code"], "INVALID_CURSOR")
        self.assertEqual(len(self.api.calls), 2)

    def test_later_page_error_discards_partial_collection_and_redacts(self) -> None:
        def respond() -> None:
            if len(self.api.calls) == 1:
                self.api.response = {"sources": [{"id": "s-1"}], "next_cursor": "next"}
            else:
                self.api.status = 403
                self.api.response = {"error": {"message": TOKEN}}

        self.api.on_request = respond
        code, out, err = self.invoke("sources", "--all")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err)["error"]["status"], 403)
        self.assertEqual(len(self.api.calls), 2)

    def test_all_pages_has_aggregate_size_bound(self) -> None:
        self.api.response = {"sources": [{"id": "s-1"}], "next_cursor": None}
        with patch.object(hosted, "MAX_COLLECTION_BYTES", 1):
            code, out, err = self.invoke("sources", "--all")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err)["error"]["code"], "PAGINATION_SIZE_LIMIT")

    def test_scalar_reads_do_not_accept_pagination_flags(self) -> None:
        for command in ("status", "context", "reports"):
            with self.subTest(command=command), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
                cli.build_parser().parse_args(["hosted", command, "--all"])
            self.assertEqual(caught.exception.code, 2)

    def test_path_traversal_ids_rejected(self) -> None:
        for value in ("..", ".", "a/b", "a\\b", "%2f", "a\n", ""):
            with self.subTest(value=value):
                self.assertEqual(self.invoke("command-result", value)[0], 1)
                self.assertEqual(self.invoke("configure", "--endpoint", "https://example.test", "--company", value)[0], 1)
        self.assertFalse(self.api.calls)

    def test_endpoint_validation(self) -> None:
        invalid = ("http://example.test", "http://localhost.evil.test", "http://127.1", "http://0.0.0.0",
                   "http://127.0.0.2", "ftp://localhost", "https://user:secret@example.test", "https://example.test?x=1",
                   "https://example.test/#fragment", "https://example.test/api/v2", "https://example.test:bad",
                   "https://example.test\\evil", "https://example.test\n", "//example.test")
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                code, _, _ = self.invoke("configure", "--endpoint", endpoint, "--company", "A", "--allow-localhost")
                self.assertEqual(code, 1)
        for endpoint in ("https://example.test", "https://example.test/api/v1/", "https://example.test:8443"):
            self.assertEqual(self.invoke("configure", "--endpoint", endpoint, "--company", "A")[0], 0)
        self.assertFalse(self.api.calls)

    def test_local_http_requires_explicit_opt_in_and_is_revalidated(self) -> None:
        self.assertEqual(self.invoke("configure", "--endpoint", self.api.endpoint, "--company", "A")[0], 1)
        for endpoint in ("http://localhost:8000", "http://[::1]:8000", self.api.endpoint):
            self.assertEqual(self.invoke("configure", "--endpoint", endpoint, "--company", "A", "--allow-localhost")[0], 0)
        config = json.loads(self.config.read_text())
        config["allow_localhost"] = False
        self.config.write_text(json.dumps(config))
        self.assertEqual(self.invoke("status")[0], 1)
        self.assertFalse(self.api.calls)

    def test_all_redirects_blocked_for_reads_and_posts(self) -> None:
        other = LocalAPI()
        self.addCleanup(other.close)
        path = self.file(self.body())
        for status in (301, 302, 303, 307, 308):
            for location in (other.endpoint + "/stolen", self.api.endpoint + "/elsewhere", "https://example.test/stolen"):
                with self.subTest(status=status, location=location):
                    self.api.status, self.api.location = status, location
                    before = len(self.api.calls)
                    for args in (("status",), ("command", "--file", path)):
                        code, out, err = self.invoke(*args)
                        self.assertEqual((code, out), (1, ""))
                        self.assertEqual(json.loads(err.splitlines()[-1])["error"]["code"], "REDIRECT_BLOCKED")
                    self.assertEqual(len(self.api.calls), before + 2)
        self.assertFalse(other.calls)

    def test_aliases_preserve_payload_explanation_and_run_context(self) -> None:
        for alias, operation in hosted.OPERATIONS.items():
            body = self.body(operation)
            body["run_id"] = "run-1"
            if operation in {"run.checkpoint", "run.finish"}:
                body["payload"] = {"run_id": "run-1", "summary": "Sources inspected", "next_action": "Review evidence"}
            source = dict(body)
            del source["operation"]
            with self.subTest(alias=alias):
                code, out, _ = self.invoke(alias, "--file", self.file(source), "--idempotency-key", alias)
                self.assertEqual(code, 0)
                call = self.api.calls[-1]
                self.assertEqual(call["body"], body)
                self.assertEqual(call["path"], "/api/v1/companies/company-A/commands")
                self.assertEqual(call["headers"]["Content-Type"], "application/json")
                self.assertEqual(json.loads(out)["idempotency_key"], alias)

    def test_generated_receipt_is_private_and_persisted_before_submission(self) -> None:
        observed = []

        def observe() -> None:
            observed.extend(json.loads(path.read_text()) for path in self.receipts())

        self.api.on_request = observe
        code, out, _ = self.invoke("command", "--file", self.file(self.body()))
        self.assertEqual(code, 0)
        self.assertEqual(observed[0]["status"], "pending")
        result = json.loads(out)
        self.assertEqual(observed[0]["idempotency_key"], result["idempotency_key"])
        receipt = Path(result["local_receipt"])
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        self.assertEqual(json.loads(receipt.read_text())["status"], "received")
        self.assertNotIn(TOKEN, receipt.read_text())

    def test_timeout_then_saved_receipt_retry_reuses_exact_key_and_body(self) -> None:
        path = self.file(self.body())
        self.api.delay = 0.1
        code, out, err = self.invoke("command", "--file", path, timeout="0.02")
        self.assertEqual((code, out), (1, ""))
        failure = json.loads(err.splitlines()[-1])["error"]
        self.assertEqual(failure["code"], "TRANSPORT_ERROR")
        receipt = Path(failure["local_receipt"])
        self.assertEqual(json.loads(receipt.read_text())["status"], "pending")
        self.assertEqual(len(self.api.calls), 1)
        Path(path).unlink()
        self.api.delay = 0
        code, _, _ = self.invoke("retry", "--receipt", str(receipt))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(self.api.calls[0], self.api.calls[1])

    def test_explicit_key_retry_and_changed_body_conflict(self) -> None:
        path = self.file(self.body())
        for _ in range(2):
            self.assertEqual(self.invoke("command", "--file", path, "--idempotency-key", "stable-key")[0], 0)
        body = self.body()
        body["payload"]["purpose"] = "Changed"
        code, _, err = self.invoke("command", "--file", self.file(body), "--idempotency-key", "stable-key")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err)["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(len(self.receipts()), 1)

    def test_receipt_retry_refuses_different_company_endpoint_or_credential(self) -> None:
        _, out, _ = self.invoke("command", "--file", self.file(self.body()))
        receipt = json.loads(out)["local_receipt"]
        original = self.config.read_text()
        for field, value in (("company", "company-B"), ("endpoint", "https://different.example.test")):
            config = json.loads(original)
            config[field] = value
            self.config.write_text(json.dumps(config))
            code, _, err = self.invoke("retry", "--receipt", receipt)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(err)["error"]["code"], "RECEIPT_SCOPE_MISMATCH")
        self.config.write_text(original)
        with patch.dict(os.environ, {"BOOKS_API_TOKEN": "different-credential"}):
            self.assertEqual(self.invoke("retry", "--receipt", receipt)[0], 1)
        self.assertEqual(len(self.api.calls), 1)

    def test_receipt_failure_prevents_submission(self) -> None:
        with patch.object(hosted, "_write_private", side_effect=OSError("secret path " + TOKEN)):
            code, out, err = self.invoke("command", "--file", self.file(self.body()))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err)["error"]["code"], "LOCAL_ERROR")
        self.assertFalse(self.api.calls)

    def test_server_errors_are_actionable_nonzero_and_redacted(self) -> None:
        for status in (401, 403, 404, 409, 422, 500):
            self.api.status = status
            self.api.response = {"error": {"code": "SERVER_CODE", "message": "echo " + TOKEN,
                                           "details": {"Authorization": "Bearer " + TOKEN, "token": "other-secret"}}}
            code, out, err = self.invoke("status")
            self.assertEqual((code, out), (1, ""))
            result = json.loads(err)["error"]
            self.assertEqual(result["status"], status)
            self.assertEqual(result["server_error"]["error"]["code"], "SERVER_CODE")
            self.assertNotIn("other-secret", err)
            if status == 403:
                self.assertIn("human staff", result["message"])

    def test_confirm_does_not_invent_authority_or_fallback(self) -> None:
        self.api.status = 403
        self.api.response = {"error": {"code": "HUMAN_APPROVAL_REQUIRED", "message": "Agent cannot post"}}
        body = self.body("queue.confirm")
        code, out, err = self.invoke("confirm", "--file", self.file(body))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("HUMAN_APPROVAL_REQUIRED", err)
        self.assertEqual(self.api.calls[0]["body"], body)
        self.assertEqual(len(self.api.calls), 1)

    def test_success_error_envelope_and_non_json_responses(self) -> None:
        self.api.response = {"error": {"message": TOKEN}}
        self.assertEqual(self.invoke("status")[0], 1)
        for raw in (b"<html>not JSON</html>", b"[]", b"null", b"{\"x\":NaN}", b"{\"x\":1e999}", b"\xff"):
            self.api.raw = raw
            code, _, err = self.invoke("status")
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(err)["error"]["code"], "INVALID_RESPONSE")

    def test_html_error_body_not_echoed(self) -> None:
        self.api.status = 500
        self.api.raw = ("<html>" + TOKEN + " another-secret</html>").encode()
        code, _, err = self.invoke("status")
        self.assertEqual(code, 1)
        self.assertNotIn("another-secret", err)

    def test_import_source_posts_deduplication_id_and_idempotency_header(self) -> None:
        source = {"source_id": "synthetic-1", "date": "2026-09-01", "description": "Synthetic",
                  "amount": "2.00", "account": "Assets:Cash", "evidence_id": "evidence-1"}
        for _ in range(2):
            self.assertEqual(self.invoke("import-source", "--file", self.file(source), "--idempotency-key", "source-key")[0], 0)
        self.assertEqual(self.api.calls[-1]["path"], "/api/v1/companies/company-A/sources")
        self.assertEqual(self.api.calls[-1]["body"], source)
        self.assertEqual(self.api.calls[-1]["headers"]["Idempotency-Key"], "source-key")
        del source["source_id"]
        self.assertEqual(self.invoke("import-source", "--file", self.file(source))[0], 1)
        self.assertEqual(len(self.api.calls), 2)

    def test_bad_commands_fail_before_submission(self) -> None:
        invalid = [{"operation": "approve", "payload": {}}, {"operation": "run.start", "payload": []},
                   {**self.body(), "approved_by": "admin"}, self.body("queue.propose")]
        del invalid[-1]["explanation"]
        for revision in (None, True, -1, "1"):
            invalid.append({**self.body("queue.confirm"), "expected_books_revision": revision})
        for body in invalid:
            self.assertEqual(self.invoke("command", "--file", self.file(body))[0], 1)
        self.assertEqual(self.invoke("propose", "--file", self.file(self.body()))[0], 1)
        self.assertFalse(self.api.calls)

    def test_credentials_in_inputs_rejected_without_persisting(self) -> None:
        for secret in (TOKEN, {"authorization": "Bearer another-secret"}):
            body = self.body()
            body["payload"]["purpose"] = secret
            self.assertEqual(self.invoke("command", "--file", self.file(body))[0], 1)
        self.assertEqual(self.invoke("command", "--file", self.file(self.body()), "--idempotency-key", TOKEN)[0], 1)
        self.assertFalse(self.api.calls)
        self.assertFalse(self.receipts())

    def test_invalid_timeout_and_header_values(self) -> None:
        for timeout in ("0", "-1", "nan", "inf"):
            self.assertEqual(self.invoke("status", timeout=timeout)[0], 1)
        for key in ("bad\r\nX: yes", "", "x" * 201):
            self.assertEqual(self.invoke("command", "--file", self.file(self.body()), "--idempotency-key", key)[0], 1)
        with patch.dict(os.environ, {"BOOKS_API_TOKEN": "bad\r\nX: yes"}):
            self.assertEqual(self.invoke("status")[0], 1)
        self.assertFalse(self.api.calls)

    def test_symlink_config_and_receipt_not_followed(self) -> None:
        real = self.directory / "real.json"
        self.config.rename(real)
        self.config.symlink_to(real)
        self.assertEqual(self.invoke("status")[0], 1)
        self.config.unlink()
        real.rename(self.config)
        _, out, _ = self.invoke("command", "--file", self.file(self.body()))
        receipt = Path(json.loads(out)["local_receipt"])
        link = self.directory / "linked-receipt.json"
        link.symlink_to(receipt)
        self.assertEqual(self.invoke("retry", "--receipt", str(link))[0], 1)
        self.assertEqual(len(self.api.calls), 1)

    def test_receipt_route_cannot_escape_company_prefix(self) -> None:
        _, out, _ = self.invoke("command", "--file", self.file(self.body()))
        receipt = Path(json.loads(out)["local_receipt"])
        body = json.loads(receipt.read_text())
        for route in ("https://example.test", "/../companies/B/commands", "//example.test", "/commands?company=B"):
            body["route"] = route
            receipt.write_text(json.dumps(body))
            self.assertEqual(self.invoke("retry", "--receipt", str(receipt))[0], 1)
        self.assertEqual(len(self.api.calls), 1)

    def test_changed_receipt_body_is_not_submitted(self) -> None:
        _, out, _ = self.invoke("command", "--file", self.file(self.body()))
        receipt = Path(json.loads(out)["local_receipt"])
        body = json.loads(receipt.read_text())
        body["body"]["payload"]["purpose"] = "Accidentally edited"
        receipt.write_text(json.dumps(body))
        code, _, err = self.invoke("retry", "--receipt", str(receipt))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err)["error"]["code"], "INVALID_RECEIPT")
        self.assertEqual(len(self.api.calls), 1)

    def test_connection_close_and_body_timeout_keep_pending_receipt(self) -> None:
        for mode in ("drop", "body_delay"):
            with self.subTest(mode=mode):
                self.api.drop = mode == "drop"
                self.api.body_delay = 0.1 if mode == "body_delay" else 0
                code, out, err = self.invoke("command", "--file", self.file(self.body()), timeout="0.02")
                self.assertEqual((code, out), (1, ""))
                failure = json.loads(err.splitlines()[-1])["error"]
                self.assertEqual(failure["code"], "TRANSPORT_ERROR")
                self.assertEqual(json.loads(Path(failure["local_receipt"]).read_text())["status"], "pending")
        self.assertEqual(len(self.api.calls), 2)

    def test_response_receipt_write_failure_preserves_retry_coordinates(self) -> None:
        write_private = hosted._write_private

        def fail_completion(path, value, *, exclusive=False):
            if not exclusive:
                raise OSError("synthetic full disk")
            return write_private(path, value, exclusive=exclusive)

        with patch.object(hosted, "_write_private", side_effect=fail_completion):
            code, out, err = self.invoke("command", "--file", self.file(self.body()))
        self.assertEqual((code, out), (1, ""))
        failure = json.loads(err.splitlines()[-1])["error"]
        self.assertEqual(failure["code"], "RECEIPT_WRITE_FAILED")
        receipt = json.loads(Path(failure["local_receipt"]).read_text())
        self.assertEqual(receipt["status"], "pending")
        self.assertEqual(receipt["idempotency_key"], self.api.calls[0]["headers"]["Idempotency-Key"])

    def test_environment_proxy_cannot_receive_authorization(self) -> None:
        proxy = LocalAPI()
        self.addCleanup(proxy.close)
        with patch.dict(os.environ, {"http_proxy": proxy.endpoint, "HTTP_PROXY": proxy.endpoint, "no_proxy": ""}):
            self.assertEqual(self.invoke("status")[0], 0)
        self.assertFalse(proxy.calls)
        self.assertEqual(len(self.api.calls), 1)

    def test_alternate_secret_field_names_are_redacted_in_responses(self) -> None:
        self.api.response = {key: "never-echo-this" for key in (
            "apiToken", "api_key", "access_token", "refreshToken", "client_secret", "Set-Cookie", "Proxy-Authorization",
        )}
        code, out, _ = self.invoke("status")
        self.assertEqual(code, 0)
        self.assertNotIn("never-echo-this", out)

    def test_https_never_downgrades_to_plaintext(self) -> None:
        config = json.loads(self.config.read_text())
        config["endpoint"] = config["endpoint"].replace("http://", "https://")
        self.config.write_text(json.dumps(config))
        code, out, err = self.invoke("status")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(json.loads(err)["error"]["code"], "TRANSPORT_ERROR")
        self.assertFalse(self.api.calls)

    def test_entity_binding_does_not_replace_global_configuration(self) -> None:
        original = self.config.read_bytes()
        entity = self.directory / "company"
        code, out, _ = self.invoke("configure", "--endpoint", self.api.endpoint, "--company", "company-B", "--allow-localhost", "--entity", str(entity))
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_bytes(), original)
        binding = entity / ".slashbooks-remote.json"
        self.assertEqual(stat.S_IMODE(binding.stat().st_mode), 0o600)
        self.assertEqual(json.loads(binding.read_text())["company"], "company-B")
        self.assertEqual(json.loads(out)["company"], "company-B")
        self.assertFalse(self.api.calls)

    def test_environment_token_reference_is_explicit_and_never_falls_back(self) -> None:
        self.assertEqual(self.invoke("configure", "--endpoint", self.api.endpoint, "--company", "company-A", "--allow-localhost", "--tokenref", "env:COMPANY_TOKEN")[0], 0)
        self.assertEqual(self.invoke("status")[0], 1)
        self.assertFalse(self.api.calls)
        with patch.dict(os.environ, {"COMPANY_TOKEN": "synthetic-company-token"}):
            self.assertEqual(self.invoke("status")[0], 0)
        self.assertEqual(self.api.calls[-1]["headers"]["Authorization"], "Bearer synthetic-company-token")

    def test_invalid_or_ambiguous_token_reference_is_rejected(self) -> None:
        for extra in (("--tokenref", "shell:cat token"), ("--tokenref", "env:TOKEN", "--store-token")):
            self.assertEqual(self.invoke("configure", "--endpoint", self.api.endpoint, "--company", "A", "--allow-localhost", *extra)[0], 1)

    def test_encoded_request_limit_is_checked_before_transport(self) -> None:
        client = hosted.HostedClient(json.loads(self.config.read_text()))
        body = {"payload": "encoded"}
        exact_size = len(json.dumps(body, sort_keys=True, allow_nan=False).encode())
        with patch.object(hosted, "MAX_REQUEST_BYTES", exact_size - 1):
            with self.assertRaises(hosted.HostedError) as caught:
                client.request("/engine/commands", body=body, key="synthetic-key")
        self.assertEqual(caught.exception.payload["error"]["code"], "REQUEST_TOO_LARGE")
        self.assertFalse(self.api.calls)
        with patch.object(hosted, "MAX_REQUEST_BYTES", exact_size):
            self.assertEqual(client.request("/engine/commands", body=body, key="synthetic-key"), {"ok": True})

    def test_artifact_payload_is_not_corrupted_by_recursive_redaction(self) -> None:
        client = hosted.HostedClient(json.loads(self.config.read_text()))
        self.api.response = {"stdout": TOKEN, "artifacts": [{"path": "@output/report", "data_base64": TOKEN}]}
        result = client.request("/engine/commands", body={"argv": []}, key="synthetic-key", preserve_artifacts=True)
        self.assertEqual(result["stdout"], "[REDACTED]")
        self.assertEqual(result["artifacts"][0]["data_base64"], TOKEN)


class CloudEndpointDefaultTest(unittest.TestCase):
    def test_login_uses_the_official_cloud_unless_another_server_is_given(self) -> None:
        parser = cli.build_parser()
        default = parser.parse_args(["hosted", "login", "--company", "company", "--entity", "/tmp/books"])
        self.assertEqual(default.endpoint, "https://slashbooks.co")
        custom = parser.parse_args(["hosted", "login", "--endpoint", "https://books.example.com",
                                    "--company", "company", "--entity", "/tmp/books"])
        self.assertEqual(custom.endpoint, "https://books.example.com")


if __name__ == "__main__":
    unittest.main()
