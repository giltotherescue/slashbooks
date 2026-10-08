"""Synthetic offline snapshots and loopback HTTP only; no deployed-service claims."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import stat
import struct
import sys
import threading
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bookkeeping import cli, hosted, hosted_adoption as adoption, migration_bundle as bundle
from bookkeeping import hosted_migration as capture
from tests import test_hosted_migration as fixtures

TOKEN = "synthetic-adoption-token-never-print"


class LocalAPI:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.response = {}
        self.raw = None
        self.drop = False
        self.location = None
        self.on_request = None
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fixture.calls.append({"path": self.path, "headers": dict(self.headers), "raw": raw})
                if fixture.on_request:
                    fixture.on_request()
                if fixture.drop:
                    self.close_connection = True
                    return
                encoded = fixture.raw if fixture.raw is not None else json.dumps(fixture.response).encode()
                self.send_response(fixture.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                if fixture.location:
                    self.send_header("Location", fixture.location)
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.endpoint = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class HostedAdoptionTests(unittest.TestCase):
    write = fixtures.MigrationPreflightTests.write
    add_entry = fixtures.MigrationPreflightTests.add_entry

    def setUp(self):
        fixtures.MigrationPreflightTests.setUp(self)
        if not any(event["type"] == "ledger-store-sealed" for event in self.store.load_audit_events()):
            with self.store.transaction() as sql:
                self.store.append_audit_event("ledger-store-sealed", {"store_sha256": self.store.content_digest(sql)}, sql)
        self.directory = self.root.parent
        self.config = self.directory / "hosted.json"
        self.source = self.directory / "synthetic.zip"
        self.write("entity.json", {"name": "Synthetic \u00e9 Company", "bank_account_mappings": {}, "related_entities": []})
        self.write("reports/synthetic.json", {"note": "Synthetic evidence only"})
        self.assertEqual(bundle.build_bundle(self.root, self.source)["status"], "exported")
        self.loaded = bundle.load_bundle(self.source)
        self.api = LocalAPI()
        self.addCleanup(self.api.close)
        self.env = patch.dict(os.environ, {"BOOKS_API_TOKEN": TOKEN}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.dotenv = patch.object(cli, "load_dotenv")
        self.dotenv.start()
        self.addCleanup(self.dotenv.stop)
        hosted._write_private(self.config, {"endpoint": self.api.endpoint, "company": "synthetic-company", "allow_localhost": True})
        self.api.response = self.success()

    def success(self):
        manifest = self.loaded["manifest"]
        return {"command_id": "synthetic-command-1", "books_revision": 1, "state_committed": True,
                "source_entity_sha256": manifest["source_entity"]["entity_json_sha256"],
                "manifest_sha256": hashlib.sha256(bundle._json_bytes(manifest)).hexdigest(),
                "validation_anchors": {key: manifest[key] for key in ("ledger", "reports")},
                "validation_summary": bundle.validate_files(manifest, self.loaded["files"]),
                "id_mapping": {"entries": [{"source_id": "synthetic-source", "projected_id": "synthetic-entry", "sqlite_entry_id": 1}],
                               "proposals": [], "sources": []},
                "documents": [{"id": "synthetic-evidence-" + str(index), **item}
                              for index, item in enumerate(manifest["files"])
                              if capture.is_document_path(item["path"])]}

    def invoke(self, *args, config=None):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["hosted", "--config", str(config or self.config), "--timeout", "2", *args])
        stdout, stderr = out.getvalue(), err.getvalue()
        self.assertNotIn(TOKEN, stdout + stderr)
        for line in (stdout + stderr).splitlines():
            json.loads(line)
        return code, stdout, stderr

    def adopt(self, key="synthetic-key", explanation="Adopt this verified synthetic snapshot.", path=None):
        return self.invoke("migration", "import", "--bundle", str(path or self.source), "--explanation", explanation,
                           "--idempotency-key", key)

    def receipt(self):
        paths = list((self.directory / ".slashbooks-remote-receipts").glob("*.json"))
        self.assertEqual(len(paths), 1)
        return paths[0], json.loads(paths[0].read_text())

    def assert_error(self, result, code):
        self.assertEqual(result[0], 1, result)
        self.assertEqual(result[1], "")
        last = json.loads(result[2].splitlines()[-1])["error"]
        self.assertEqual(last["code"], code, result)
        return last

    def decode_frame(self, raw):
        length = struct.unpack(">I", raw[:4])[0]
        self.assertLessEqual(length, adoption.MAX_HEADER)
        header = json.loads(raw[4:4 + length])
        self.assertEqual(set(header), {"version", "files"})
        self.assertEqual(header["version"], 1)
        cursor, files = 4 + length, {}
        for item in header["files"]:
            self.assertEqual(set(item), {"path", "size", "sha256"})
            data = raw[cursor:cursor + item["size"]]
            self.assertEqual(len(data), item["size"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
            files[item["path"]] = data
            cursor += item["size"]
        self.assertEqual(cursor, len(raw))
        return files

    def test_export_uses_no_config_credentials_or_network(self):
        output = self.directory / "offline.zip"
        with patch.dict(os.environ, {}, clear=True), patch.object(hosted, "_read_json", side_effect=AssertionError("no config")), \
                patch.object(hosted, "HostedClient", side_effect=AssertionError("no network")):
            result = self.invoke("migration", "export", "--entity-dir", str(self.root), "--output", str(output),
                                 config=self.directory / "does-not-exist.json")
        self.assertEqual(result[0], 0, result)
        self.assertEqual(json.loads(result[1])["status"], "exported")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertFalse(self.api.calls)
        self.assertFalse((self.directory / ".slashbooks-remote-receipts").exists())
        blocked = self.invoke("migration", "export", "--entity-dir", str(self.root), "--output", str(output))
        self.assertEqual(blocked[0], 1)
        self.assertEqual(json.loads(blocked[1])["blockers"], ["destination_exists"])

    def test_correct_frame_authority_free_job_and_private_receipt_exist_before_send(self):
        observed = []
        def before_response():
            path, record = self.receipt()
            snapshot = path.parent / record["snapshot"]
            observed.append((record["status"], snapshot.exists(), stat.S_IMODE(path.stat().st_mode), stat.S_IMODE(snapshot.stat().st_mode)))
        self.api.on_request = before_response
        consumed = []
        original = adoption._chunks
        def chunks(parts):
            for piece in original(parts):
                consumed.append(len(piece))
                yield piece
        with patch.object(adoption, "_chunks", side_effect=chunks):
            result = self.adopt()
        self.assertEqual(result[0], 0, result)
        self.assertEqual(observed, [("pending", True, 0o600, 0o600)])
        self.assertTrue(consumed)
        self.assertLessEqual(max(consumed), adoption.CHUNK)
        self.assertEqual(len(self.api.calls), 1)
        call = self.api.calls[0]
        self.assertEqual(call["path"], "/api/v1/companies/synthetic-company/engine/adopt")
        self.assertEqual(call["headers"]["Content-Type"], adoption.CONTENT_TYPE)
        self.assertEqual(call["headers"]["Content-Length"], str(len(call["raw"])))
        self.assertNotIn("Transfer-Encoding", call["headers"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(call["headers"]["Idempotency-Key"], "synthetic-key")
        files = self.decode_frame(call["raw"])
        self.assertEqual(list(files), ["meta/job.json", "input/manifest.json", *("entity/" + name for name in sorted(self.loaded["files"]))])
        job = json.loads(files["meta/job.json"])
        self.assertEqual(set(job), {"version", "operation", "body"})
        self.assertEqual(job["operation"], "workspace.adopt")
        self.assertEqual(job["version"], 1)
        self.assertEqual(set(job["body"]), {"expected_books_revision", "source_entity_sha256", "manifest_sha256", "explanation"})
        self.assertEqual(job["body"]["expected_books_revision"], 0)
        self.assertEqual(files["input/manifest.json"], bundle._json_bytes(self.loaded["manifest"]))
        self.assertIn(b"\\u00e9", files["input/manifest.json"])
        self.assertEqual(job["body"]["manifest_sha256"], bundle.validate_files(**self.loaded)["manifest_sha256"])
        for name, data in self.loaded["files"].items():
            self.assertEqual(files["entity/" + name], data)
        path, record = self.receipt()
        self.assertEqual(record["payload_sha256"], hashlib.sha256(call["raw"]).hexdigest())
        self.assertEqual(record["bundle_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(record["status"], "received")
        self.assertEqual(record["kind"], "engine-adoption")
        self.assertEqual(record["credential_reference"], "env:BOOKS_API_TOKEN")
        for artifact in path.parent.iterdir():
            self.assertNotIn(TOKEN.encode(), artifact.read_bytes())

    def test_uncertain_retry_uses_frozen_bytes_original_key_and_not_original_path(self):
        self.api.drop = True
        error = self.assert_error(self.adopt(), "TRANSPORT_ERROR")
        path, record = self.receipt()
        self.assertEqual(error["local_receipt"], str(path))
        self.assertEqual(record["status"], "pending")
        self.source.write_bytes(b"Changed original source must not affect intent")
        self.api.drop = False
        result = self.invoke("retry", "--receipt", str(path))
        self.assertEqual(result[0], 0, result)
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(self.api.calls[0]["raw"], self.api.calls[1]["raw"])
        self.assertEqual(self.api.calls[0]["headers"]["Idempotency-Key"], self.api.calls[1]["headers"]["Idempotency-Key"])

    def test_completed_receipt_replays_locally_and_same_import_reuses_it(self):
        self.assertEqual(self.adopt()[0], 0)
        path, record = self.receipt()
        self.assertEqual(self.adopt()[0], 0)
        self.source.unlink()
        result = self.invoke("retry", "--receipt", str(path))
        self.assertEqual(result[0], 0, result)
        self.assertEqual(json.loads(result[1])["response"], record["response"])
        self.assertEqual(len(self.api.calls), 1)

    def test_same_key_cannot_change_explanation_or_bundle(self):
        self.api.drop = True
        self.assert_error(self.adopt(), "TRANSPORT_ERROR")
        self.assert_error(self.adopt(explanation="Changed decision"), "IDEMPOTENCY_CONFLICT")
        self.write("reports/extra.json", {"synthetic": True})
        changed = self.directory / "changed.zip"
        self.assertEqual(bundle.build_bundle(self.root, changed)["status"], "exported")
        self.assert_error(self.adopt(path=changed), "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.api.calls), 1)

    def test_retry_rejects_modified_snapshot_receipt_and_completed_response(self):
        self.assertEqual(self.adopt()[0], 0)
        path, record = self.receipt()
        snapshot = path.parent / record["snapshot"]
        original = snapshot.read_bytes()
        snapshot.write_bytes(original + b"Changed archive")
        self.assert_error(self.invoke("retry", "--receipt", str(path)), "INVALID_RECEIPT")
        snapshot.write_bytes(original)
        modified = deepcopy(record)
        modified["body"]["explanation"] = "changed"
        hosted._write_private(path, modified)
        self.assert_error(self.invoke("retry", "--receipt", str(path)), "INVALID_RECEIPT")
        modified = deepcopy(record)
        modified["response"]["command_id"] = "changed"
        hosted._write_private(path, modified)
        self.assert_error(self.invoke("retry", "--receipt", str(path)), "INVALID_RECEIPT")
        self.assertEqual(len(self.api.calls), 1)

    def test_scope_and_token_reference_are_bound_without_storing_tokens(self):
        self.api.drop = True
        self.assert_error(self.adopt(), "TRANSPORT_ERROR")
        path, _ = self.receipt()
        initial = json.loads(self.config.read_text())
        for field, value in (("company", "other-company"), ("endpoint", "http://localhost:1"), ("tokenref", "env:OTHER_TOKEN")):
            with self.subTest(field=field), patch.dict(os.environ, {"OTHER_TOKEN": TOKEN}):
                hosted._write_private(self.config, {**initial, field: value})
                self.assert_error(self.invoke("retry", "--receipt", str(path)), "RECEIPT_SCOPE_MISMATCH")
        hosted._write_private(self.config, initial)
        with patch.dict(os.environ, {"BOOKS_API_TOKEN": "other-synthetic-credential"}):
            self.assert_error(self.invoke("retry", "--receipt", str(path)), "RECEIPT_SCOPE_MISMATCH")
        self.assertEqual(len(self.api.calls), 1)

    def test_response_identity_and_bounds_must_validate_before_success(self):
        mutations = [
            ("command_id", ""), ("command_id", "../bad"), ("command_id", TOKEN),
            ("books_revision", True), ("books_revision", 2), ("state_committed", False),
            ("source_entity_sha256", "0" * 64), ("manifest_sha256", "0" * 64),
            ("validation_anchors", {}), ("id_mapping", {}), ("documents", []),
            ("validation_summary", {}),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                self.api.response = {**self.success(), field: value}
                self.assert_error(self.adopt(), "INVALID_RESPONSE")
                self.assertEqual(self.receipt()[1]["status"], "pending")
        self.api.response = self.success()
        self.assertEqual(self.adopt()[0], 0)

    def test_response_mapping_and_size_bounds(self):
        with patch.object(adoption, "MAX_MAPPING_ITEMS", 0):
            self.assert_error(self.adopt(), "INVALID_RESPONSE")
        self.api.response = self.success()
        self.api.response["id_mapping"]["entries"][0]["source_id"] = "x" * 1025
        self.assert_error(self.adopt(), "INVALID_RESPONSE")
        with patch.object(hosted, "MAX_RESPONSE_BYTES", 32):
            # The request is already frozen; the bounded reader rejects an oversized result.
            path, _ = self.receipt()
            record = json.loads(path.read_text())
            client = hosted.HostedClient(json.loads(self.config.read_text()))
            with redirect_stderr(StringIO()), self.assertRaises(hosted.HostedError) as caught:
                adoption._finish(client, record, path)
            self.assertEqual(caught.exception.payload["error"]["code"], "INVALID_RESPONSE")

    def test_transfer_bounds_fail_before_any_send(self):
        cases = [(capture, "MAX_FILE", 1), (capture, "MAX_TOTAL", 1),
                 (capture, "MAX_ITEMS", 1), (bundle, "MAX_MANIFEST", 1),
                 (adoption, "MAX_HEADER", 1), (adoption, "MAX_INTENT", 1)]
        for module, name, maximum in cases:
            with self.subTest(name=name), patch.object(module, name, maximum):
                result = self.adopt()
                self.assertEqual(result[0], 1, result)
                self.assertFalse(self.api.calls)

    def test_no_redirect_or_automatic_retry_and_http_errors_are_redacted(self):
        self.api.status = 307
        self.api.location = self.api.endpoint + "/untrusted"
        self.assert_error(self.adopt(), "REDIRECT_BLOCKED")
        self.assertEqual(len(self.api.calls), 1)
        self.api.status = 409
        self.api.response = {"error": {"code": "IDEMPOTENCY_CONFLICT", "message": TOKEN, "authorization": "other-secret"}}
        result = self.adopt()
        self.assert_error(result, "HTTP_ERROR")
        self.assertNotIn("other-secret", result[2])
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(self.receipt()[1]["status"], "pending")

    def test_success_diagnostics_are_redacted_before_persistence(self):
        self.api.response = {**self.success(), "note": TOKEN, "token": "not-the-active-secret"}
        result = self.adopt()
        self.assertEqual(result[0], 0, result)
        self.assertNotIn("not-the-active-secret", result[1])
        path, _ = self.receipt()
        self.assertNotIn(TOKEN, path.read_text())
        self.assertNotIn("not-the-active-secret", path.read_text())

    def test_registered_document_metadata_preserves_exact_source_descriptors(self):
        self.api.response["documents"][0]["id"] = "synthetic-registered-evidence"
        result = self.adopt()
        self.assertEqual(result[0], 0, result)
        self.assertEqual(json.loads(result[1])["response"]["documents"][0]["id"], "synthetic-registered-evidence")
        for field, value in (("size", 999), ("sha256", "0" * 64), ("path", "reports/replaced.json")):
            with self.subTest(field=field):
                self.api.response = self.success()
                self.api.response["documents"][0][field] = value
                self.assert_error(self.adopt(key="invalid-document-" + field), "INVALID_RESPONSE")

    def normal_document_bundle(self):
        documents = {
            "reconciliations/2026-01/check.json": b'{"synthetic":true}\n',
            "outputs/entries.ndjson": b'{"synthetic":1}\n{"synthetic":2}\n',
            "qb-exports/2026-01/trial-balance.csv": b"account,amount\nSynthetic,30.00\n",
            "audit-log.jsonl": b'{"type":"historical","actor":"original-bookkeeper"}\n',
            "bank-statement.csv": b"date,description,amount\n2026-01-02,Synthetic,25.00\n",
            "empty.csv": b"",
        }
        for name, data in documents.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.source.unlink()
        built = bundle.build_bundle(self.root, self.source)
        self.assertEqual(built["status"], "exported", built)
        self.loaded = bundle.load_bundle(self.source)
        self.api.response = self.success()
        return documents

    def test_normal_documents_are_framed_byte_exact_and_required_in_public_receipt(self):
        documents = self.normal_document_bundle()
        result = self.adopt()
        self.assertEqual(result[0], 0, result)
        response = json.loads(result[1])["response"]
        registered = {item["path"]: item for item in response["documents"]}
        self.assertEqual(set(registered), set(documents) | {"reports/synthetic.json"})
        framed = self.decode_frame(self.api.calls[0]["raw"])
        for name, data in documents.items():
            self.assertEqual(self.loaded["files"][name], data)
            self.assertEqual(framed["entity/" + name], data)
            self.assertEqual(registered[name]["size"], len(data))
            self.assertEqual(registered[name]["sha256"], hashlib.sha256(data).hexdigest())
            self.assertTrue(registered[name]["id"].startswith("synthetic-evidence-"))

    def test_cli_rejects_workspace_only_normal_documents_and_altered_descriptors(self):
        documents = self.normal_document_bundle()
        for index, name in enumerate(documents):
            with self.subTest(missing=name):
                self.api.response = self.success()
                self.api.response["documents"] = [row for row in self.api.response["documents"] if row["path"] != name]
                self.assert_error(self.adopt(key=f"missing-normal-{index}"), "INVALID_RESPONSE")
        for field, value in (("path", "outputs/replaced.ndjson"), ("size", 999), ("sha256", "0" * 64)):
            with self.subTest(field=field):
                self.api.response = self.success()
                row = next(row for row in self.api.response["documents"] if row["path"] == "outputs/entries.ndjson")
                row[field] = value
                self.assert_error(self.adopt(key="changed-normal-" + field), "INVALID_RESPONSE")

    def test_invalid_ndjson_and_custom_code_block_before_network_submission(self):
        for name, data, blocker in (("outputs/entries.ndjson", b'{"synthetic":1}\nnot-json\n', "invalid_structured_data"),
                                    ("outputs/custom.py", b"raise RuntimeError('never execute')\n", "custom_code")):
            with self.subTest(name=name):
                target = self.root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                output = self.directory / (target.name + ".zip")
                try:
                    result = self.invoke("migration", "export", "--entity-dir", str(self.root), "--output", str(output))
                    self.assertEqual(result[0], 1, result)
                    self.assertIn(blocker, json.loads(result[1])["blockers"])
                    self.assertFalse(output.exists())
                    self.assertFalse(self.api.calls)
                finally:
                    target.unlink()

    def test_intent_frame_accepts_64_kib_exactly_and_rejects_one_extra_byte(self):
        body = {"expected_books_revision": 0, "explanation": "",
                "source_entity_sha256": self.api.response["source_entity_sha256"],
                "manifest_sha256": self.api.response["manifest_sha256"]}
        overhead = len(bundle._json_bytes({"version": 1, "operation": "workspace.adopt", "body": body}))
        body["explanation"] = "x" * (adoption.MAX_INTENT - overhead)
        frame = adoption._frame(self.loaded, body)
        self.assertEqual(len(frame[2]), 64 * 1024)
        body["explanation"] += "x"
        with self.assertRaises(hosted.HostedError) as caught:
            adoption._frame(self.loaded, body)
        self.assertEqual(caught.exception.payload["error"]["code"], "ADOPTION_LIMIT")
        self.assertFalse(self.api.calls)

    def test_public_receipt_requires_summary_and_safe_unique_registered_document_ids(self):
        self.write("reports/second.json", {"synthetic": True})
        self.source.unlink()
        self.assertEqual(bundle.build_bundle(self.root, self.source)["status"], "exported")
        self.loaded = bundle.load_bundle(self.source)
        for case in ("missing-summary", "missing-id", "empty-id", "unsafe-id", "large-id", "duplicate-id", "extra-field", "duplicate-path", "extra-document"):
            with self.subTest(case=case):
                response = self.success()
                docs = response["documents"]
                if case == "missing-summary":
                    del response["validation_summary"]
                elif case == "missing-id":
                    del docs[0]["id"]
                elif case == "empty-id":
                    docs[0]["id"] = ""
                elif case == "unsafe-id":
                    docs[0]["id"] = "../unsafe"
                elif case == "large-id":
                    docs[0]["id"] = "x" * 201
                elif case == "duplicate-id":
                    docs[1]["id"] = docs[0]["id"]
                elif case == "extra-field":
                    docs[0]["other"] = "unexpected"
                elif case == "duplicate-path":
                    docs[1] = {**docs[0], "id": docs[1]["id"]}
                else:
                    docs.append({"id": "extra", "path": "reports/extra.json", "size": 0, "sha256": "0" * 64})
                self.api.response = response
                self.assert_error(self.adopt(), "INVALID_RESPONSE")
                self.assertEqual(self.receipt()[1]["status"], "pending")
        self.api.response = self.success()
        self.assertEqual(self.adopt()[0], 0)

    def test_bundle_change_during_capture_blocks_before_freezing_or_sending(self):
        original = capture._signature
        modified = False
        def signature(info):
            nonlocal modified
            if not modified:
                modified = True
                self.source.write_bytes(self.source.read_bytes() + b"changed")
            return original(info)
        with patch.object(capture, "_signature", side_effect=signature):
            self.assert_error(self.adopt(), "INPUT_CHANGED")
        self.assertFalse(self.api.calls)
        self.assertFalse((self.directory / ".slashbooks-remote-receipts").exists())

    def test_invalid_json_outcome_stays_pending_and_generated_key_is_reused(self):
        self.api.raw = b"Private invalid server diagnostic " + TOKEN.encode()
        result = self.invoke("migration", "import", "--bundle", str(self.source), "--explanation", "Synthetic import")
        self.assert_error(result, "INVALID_RESPONSE")
        path, record = self.receipt()
        self.assertEqual(record["status"], "pending")
        self.assertTrue(record["idempotency_key"])
        self.assertEqual(len(self.api.calls), 1)
        self.api.raw = None
        self.assertEqual(self.invoke("retry", "--receipt", str(path))[0], 0)
        self.assertEqual(self.api.calls[1]["headers"]["Idempotency-Key"], record["idempotency_key"])

    def test_missing_auth_or_invalid_explanation_does_not_send(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assert_error(self.adopt(), "AUTH_REQUIRED")
        for explanation in ("", "  ", "x" * 4001):
            self.assert_error(self.adopt(explanation=explanation), "INVALID_EXPLANATION")
        self.assertFalse(self.api.calls)

    def test_credentials_in_intent_or_snapshot_never_publish_or_send(self):
        self.assert_error(self.adopt(explanation=TOKEN), "SECRET_IN_INPUT")
        self.assert_error(self.adopt(key=TOKEN), "SECRET_IN_INPUT")
        self.write("reports/synthetic-note.json", {"note": TOKEN})
        source = self.directory / "credential.zip"
        self.assertEqual(bundle.build_bundle(self.root, source)["status"], "exported")
        self.assert_error(self.adopt(path=source), "SECRET_IN_INPUT")
        self.assertFalse(self.api.calls)
        self.assertFalse((self.directory / ".slashbooks-remote-receipts").exists())

    def test_private_snapshot_and_receipt_permissions_and_links_are_checked(self):
        self.api.drop = True
        self.assert_error(self.adopt(), "TRANSPORT_ERROR")
        path, record = self.receipt()
        snapshot = path.parent / record["snapshot"]
        snapshot.chmod(0o644)
        self.assert_error(self.invoke("retry", "--receipt", str(path)), "INSECURE_FILE")
        snapshot.chmod(0o600)
        original = snapshot.read_bytes()
        snapshot.unlink()
        snapshot.symlink_to(self.source)
        self.assertEqual(self.invoke("retry", "--receipt", str(path))[0], 1)
        snapshot.unlink()
        snapshot.write_bytes(original)
        snapshot.chmod(0o600)
        path.chmod(0o644)
        self.assert_error(self.invoke("retry", "--receipt", str(path)), "INSECURE_FILE")
        self.assertEqual(len(self.api.calls), 1)

    def test_noncanonical_archive_manifest_is_sent_as_the_canonical_hash_bytes(self):
        output = BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.json", json.dumps(self.loaded["manifest"], ensure_ascii=False, indent=2))
            for name, data in self.loaded["files"].items():
                archive.writestr("entity/" + name, data)
        alternate = self.directory / "noncanonical.zip"
        alternate.write_bytes(output.getvalue())
        result = self.adopt(path=alternate)
        self.assertEqual(result[0], 0, result)
        files = self.decode_frame(self.api.calls[-1]["raw"])
        self.assertEqual(files["input/manifest.json"], bundle._json_bytes(self.loaded["manifest"]))
        self.assertEqual(hashlib.sha256(files["input/manifest.json"]).hexdigest(), self.api.response["manifest_sha256"])

    def test_receipt_write_failure_prevents_send_and_postresponse_failure_keeps_retry(self):
        original = hosted._write_private
        with patch.object(hosted, "_write_private", side_effect=OSError("private disk diagnostic " + TOKEN)):
            result = self.adopt()
        self.assertEqual(result[0], 1)
        self.assertFalse(self.api.calls)
        def fail_completion(path, record, **kwargs):
            if record.get("status") == "received":
                raise OSError("private disk diagnostic " + TOKEN)
            return original(path, record, **kwargs)
        with patch.object(hosted, "_write_private", side_effect=fail_completion):
            self.assert_error(self.adopt(), "LOCAL_RECOVERY_REQUIRED")
        path, record = self.receipt()
        self.assertEqual(record["status"], "pending")
        self.assertEqual(self.invoke("retry", "--receipt", str(path))[0], 0)
        self.assertEqual(self.api.calls[0]["raw"], self.api.calls[1]["raw"])


if __name__ == "__main__":
    unittest.main()
