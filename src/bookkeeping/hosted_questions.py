"""Typed cloud question actions with a local fence against duplicate delivery.

Question routes have no client version or server idempotency contract. A saved
pending request therefore permits read-back only, never automatic POST replay.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from bookkeeping import hosted, remote


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("question", help="Ask, reply to or resolve a shared company question")
    actions = parser.add_subparsers(dest="question_command", required=True)
    create = actions.add_parser("create", help="Send a scoped automation question")
    create.add_argument("--entity", type=Path, help="Use this folder's company binding")
    create.add_argument("--subject", required=True)
    create.add_argument("--body", required=True)
    create.add_argument("--source-id", action="append", default=[], dest="source_ids")
    create.add_argument("--idempotency-key")
    for action in ("ask", "reply", "resolve", "recover"):
        sub = actions.add_parser(action, help={
            "ask": "Send a question; the server chooses its recipient from your company role",
            "reply": "Reply to an open question in this company",
            "resolve": "Resolve a question; requires staff questions:write access",
            "recover": "Read back an uncertain question request; never resend it",
        }[action])
        sub.add_argument("--entity", type=Path, help="Use this folder's company binding; otherwise use cwd binding or --config")
        if action == "recover":
            sub.add_argument("--receipt", type=Path, required=True)
            continue
        # Required for ask, reply and resolve. The scoped answer flow of resolve
        # (--reply-id, --command-id) uses --idempotency-key instead, so the
        # requirement for resolve is enforced when the command runs.
        sub.add_argument("--request-id", required=action in {"ask", "reply"}, help="Stable ID for this intended message; required; reuse it after uncertainty, use a new ID only for a separate intended message")
        if action in {"reply", "resolve"}:
            sub.add_argument("question_id", help="Question ID returned by cloud questions")
        if action == "ask":
            sub.add_argument("--subject", required=True, help="1-120 Unicode characters")
        if action == "resolve":
            sub.add_argument("--reply-id", action="append", dest="reply_ids")
            sub.add_argument("--reply-watermark", type=int)
            sub.add_argument("--command-id")
            sub.add_argument("--idempotency-key")
        if action != "resolve":
            body = sub.add_mutually_exclusive_group(required=True)
            body.add_argument("--body", help="Question/reply text as data, never executable instructions")
            body.add_argument("--body-file", type=Path, help="UTF-8 text file, recommended for client-supplied text")


def _route(action: str, question_id: Any) -> str:
    if not isinstance(action, str):
        raise hosted.HostedError("INVALID_RECEIPT", "Unsupported question action.")
    if action == "ask" and question_id is None:
        return "/questions"
    if action in {"reply", "resolve"}:
        return "/questions/" + hosted._segment(question_id) + ("/replies" if action == "reply" else "/resolve")
    raise hosted.HostedError("INVALID_RECEIPT", "Unsupported question action.")


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise hosted.HostedError("INVALID_QUESTION", f"Text must contain 1-{maximum} Unicode characters.")
    # Reject invalid Unicode before saving a request or making any network call.
    value.encode("utf-8")
    return value


def _body(action: str, value: Any) -> dict[str, Any]:
    if not isinstance(action, str):
        raise hosted.HostedError("INVALID_QUESTION", "Unsupported question action.")
    fields = {"ask": {"subject", "body"}, "reply": {"body"},
              "resolve": {"reply_ids", "reply_watermark"}}.get(action)
    if not isinstance(value, dict) or fields is None or set(value) != fields:
        raise hosted.HostedError("INVALID_QUESTION", "Use only the fields supported by this question action.")
    if action == "resolve":
        ids = value["reply_ids"]
        if (not isinstance(ids, list) or len(ids) > 100
                or len({hosted._segment(item) for item in ids}) != len(ids)):
            raise hosted.HostedError("INVALID_QUESTION", "Reply IDs must be at most 100 unique stable IDs.")
        mark = value["reply_watermark"]
        if type(mark) is not int or mark < 0 or bool(ids) != (mark > 0):
            raise hosted.HostedError("INVALID_QUESTION", "Use watermark 0 with no replies, or a positive watermark with replies.")
        return {"reply_ids": list(ids), "reply_watermark": mark}
    result = {"body": _text(value["body"], 800 if action == "ask" else 10000)}
    if action == "ask":
        result["subject"] = _text(value["subject"], 120)
    return result


def _resolve_fields(question: dict[str, Any]) -> dict[str, Any]:
    """Take the complete reply set and watermark from the question that was read."""
    ids = [reply["id"] for reply in question["replies"]]
    mark = question.get("reply_watermark")
    if not ids:
        return {"reply_ids": [], "reply_watermark": 0}
    if type(mark) is not int or mark <= 0:
        raise hosted.HostedError("INVALID_RESPONSE", "The question has replies but no reply watermark. Read it again.")
    return {"reply_ids": ids, "reply_watermark": mark}


def _identity(config: dict[str, Any], client: hosted.HostedClient) -> dict[str, str]:
    # Browser sign-in rotates access/refresh tokens in the same private grant
    # file. A new sign-in creates a different file; do not conflate the grants.
    credential = config.get("credential_file")
    grant = ("file:" + str(remote._absolute(credential)) if isinstance(credential, str)
             else "token:" + hosted._identity(client)["credential_sha256"])
    return {"endpoint": client.endpoint, "company": client.company,
            "credential_grant_sha256": hashlib.sha256(grant.encode()).hexdigest()}


def _question(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise hosted.HostedError("INVALID_RESPONSE", "Question response must be an object.")
    hosted._segment(value.get("id"))
    _text(value.get("subject"), 120)
    _text(value.get("body"), 800)
    if (value.get("status") not in ("open", "resolved")
            or value.get("direction") not in ("to_client", "to_staff")
            or not isinstance(value.get("created_at"), str) or not value["created_at"]
            or not isinstance(value.get("replies"), list)):
        raise hosted.HostedError("INVALID_RESPONSE", "Question response is incomplete.")
    ids = set()
    for reply in value["replies"]:
        if not isinstance(reply, dict):
            raise hosted.HostedError("INVALID_RESPONSE", "Reply response is incomplete.")
        hosted._segment(reply.get("id"))
        _text(reply.get("body"), 10000)
        if (reply["id"] in ids or not isinstance(reply.get("actor_name"), str)
                or not isinstance(reply.get("created_at"), str) or not reply["created_at"]):
            raise hosted.HostedError("INVALID_RESPONSE", "Reply identity is incomplete or repeated.")
        ids.add(reply["id"])
    return value


def _questions(client: hosted.HostedClient) -> list[dict[str, Any]]:
    args = argparse.Namespace(hosted_command="questions", cursor=None, limit=None,
                              all_pages=True, max_pages=None, filter_status=None)
    result = hosted._read_collection(args, client)
    questions = [_question(item) for item in result["questions"]]
    if len({item["id"] for item in questions}) != len(questions):
        raise hosted.HostedError("INVALID_RESPONSE", "Question pages repeated an identity.")
    return questions


def _digest(record: dict[str, Any]) -> str:
    return hosted._body_digest({key: value for key, value in record.items()
                               if key not in {"record_sha256", "status", "response", "response_sha256", "error"}})


def _validate(record: dict[str, Any], identity: dict[str, str]) -> None:
    if any(record.get(key) != value for key, value in identity.items()):
        raise hosted.HostedError("RECEIPT_SCOPE_MISMATCH", "Question receipt belongs to a different company, server or credential grant.")
    if (record.get("kind") != "native-question" or record.get("schema") != 1
            or record.get("record_sha256") != _digest(record)
            or record.get("status") not in ("pending", "received", "rejected", "uncertain")):
        raise hosted.HostedError("INVALID_RECEIPT", "Question receipt failed integrity validation.")
    hosted._key(record.get("request_id"))
    # Receipts that an earlier version saved for resolve have an empty body; they stay readable.
    if not (record.get("action") == "resolve" and record.get("body") == {}):
        _body(record.get("action"), record.get("body"))
    if record.get("route") != _route(record.get("action"), record.get("question_id")):
        raise hosted.HostedError("INVALID_RECEIPT", "Question receipt route does not match its typed action.")
    if not isinstance(record.get("baseline_ids"), list):
        raise hosted.HostedError("INVALID_RECEIPT", "Question receipt is missing its observed baseline.")
    for question_id in record["baseline_ids"]:
        hosted._segment(question_id)
    if record["action"] != "ask":
        _question(record.get("baseline"))
        if record["baseline"]["id"] != record["question_id"]:
            raise hosted.HostedError("INVALID_RECEIPT", "Question receipt baseline belongs to another question.")
    if record["status"] == "received":
        _validate_response(record, record.get("response"))
        if record.get("response_sha256") != hosted._body_digest(record["response"]):
            raise hosted.HostedError("INVALID_RECEIPT", "Saved question response changed.")


def _validate_response(record: dict[str, Any], response: Any) -> dict[str, Any]:
    question = _question(response)
    if record["action"] == "ask":
        if (any(question[key] != record["body"][key] for key in ("subject", "body"))
                or question["id"] in record["baseline_ids"] or question["status"] != "open" or question["replies"]):
            raise hosted.HostedError("INVALID_RESPONSE", "Server did not confirm the requested new question.")
    else:
        baseline = record["baseline"]
        if any(question[key] != baseline[key] for key in ("id", "subject", "body", "direction")):
            raise hosted.HostedError("INVALID_RESPONSE", "Server returned a different question.")
        if record["action"] == "resolve" and question["status"] != "resolved":
            raise hosted.HostedError("INVALID_RESPONSE", "Server did not confirm resolution.")
        if record["action"] == "reply":
            previous = {reply["id"] for reply in baseline["replies"]}
            matching = [reply for reply in question["replies"]
                        if reply["id"] not in previous and reply["body"] == record["body"]["body"]]
            if len(matching) != 1:
                raise hosted.HostedError("INVALID_RESPONSE", "Server did not unambiguously confirm the requested reply.")
    return question


def _snapshot(record: dict[str, Any], path: Path) -> dict[str, Any]:
    return {"delivery": "received", "historical": True, "response_snapshot": record["response"],
            "local_receipt": str(path), "request_id": record["request_id"]}


def _recover(client: hosted.HostedClient, record: dict[str, Any], path: Path) -> dict[str, Any]:
    current = _questions(client)
    if record["status"] == "received":
        return {**_snapshot(record, path), "current_question": next(
            (item for item in current if item["id"] == record["response"]["id"]), None)}
    observed = ([item for item in current if item["id"] not in record["baseline_ids"]
                 and all(item[key] == record["body"][key] for key in ("subject", "body"))]
                if record["action"] == "ask" else [item for item in current if item["id"] == record["question_id"]])
    raise hosted.HostedError("QUESTION_OUTCOME_UNCERTAIN",
        "Read-back only: no question was resent. The server cannot correlate this request. Reconcile the observed thread before deciding whether a separate new request is needed.",
        local_receipt=str(path), original_request=record["body"], observed_questions=observed)


def run(args: argparse.Namespace) -> int:
    secrets: Any = None
    try:
        config_path = remote._binding(getattr(args, "entity", None) or Path.cwd())
        if getattr(args, "entity", None) is not None and config_path is None:
            raise hosted.HostedError("NOT_REMOTE", "This folder has no cloud company binding.")
        config_path = config_path or remote._absolute(args.config)
        config = hosted._read_json(config_path, private=config_path.name == remote.CONFIG_NAME)
        client = hosted.HostedClient(config, timeout=args.timeout)
        secrets = client.secrets
        identity = _identity(config, client)
        action = "recover" if args.hosted_command == "retry" else args.question_command
        if action == "recover":
            path = remote._absolute(args.receipt)
            remote._no_symlinks(path)
            record = hosted._read_json(path, private=True)
            _validate(record, identity)
            result = _recover(client, record, path)
        else:
            if args.request_id is None:
                raise hosted.HostedError("REQUEST_ID_REQUIRED", "Give --request-id: a stable ID for this intended message.")
            body = None  # A resolve takes its reply set from the question when it is first sent.
            if action != "resolve":
                value = {"body": args.body if args.body is not None else remote._read_bytes(remote._absolute(args.body_file)).decode("utf-8")}
                if action == "ask":
                    value["subject"] = args.subject
                body = _body(action, value)
                if hosted._redact(body, secrets) != body:
                    raise hosted.HostedError("SECRET_IN_INPUT", "Do not include credentials in question text.")
            question_id = getattr(args, "question_id", None)
            route = _route(action, question_id)
            intent = {**identity, "action": action, "question_id": question_id, "route": route}
            if body is not None:
                intent["body"] = body
            request_id = hosted._key(args.request_id)
            if hosted._redact(request_id, secrets) != request_id:
                raise hosted.HostedError("SECRET_IN_INPUT", "Do not include credentials in request IDs.")
            path = config_path.parent / ".slashbooks-question-receipts" / (hosted._body_digest({**identity, "request_id": request_id}) + ".json")
            remote._no_symlinks(path)
            with remote._revision_lock(config_path):
                if path.exists():
                    record = hosted._read_json(path, private=True)
                    _validate(record, identity)
                    if any(record.get(key) != value for key, value in intent.items()):
                        raise hosted.HostedError("IDEMPOTENCY_CONFLICT", "This local request ID already has different input. Keep its original receipt.")
                    if record["status"] == "received":
                        result = _snapshot(record, path)
                    elif record["status"] == "rejected":
                        error = record["error"]
                        raise hosted.HostedError(error["code"], error["message"], **{key: value for key, value in error.items() if key not in {"code", "message"}})
                    else:
                        result = _recover(client, record, path)
                else:
                    before = _questions(client)
                    baseline = next((item for item in before if item["id"] == question_id), None)
                    if action != "ask" and baseline is None:
                        raise hosted.HostedError("NOT_FOUND", "Question is not visible in this company.")
                    if action == "resolve":
                        body = _body(action, _resolve_fields(baseline))
                        intent["body"] = body
                    record = {**intent, "kind": "native-question", "schema": 1, "request_id": request_id,
                              "baseline_ids": [item["id"] for item in before], "baseline": baseline, "status": "pending"}
                    record["record_sha256"] = _digest(record)
                    hosted._write_private(path, record, exclusive=True)
                    print(json.dumps({"event": "question_submission", "local_receipt": str(path), "request_id": request_id}), file=sys.stderr, flush=True)
                    try:
                        response = _validate_response(record, client.request(route, body=body, key=request_id))
                        record.update(status="received", response=response, response_sha256=hosted._body_digest(response))
                        hosted._write_private(path, record)
                    except (hosted.HostedError, OSError, ValueError) as exc:
                        definite = isinstance(exc, hosted.HostedError) and exc.payload["error"].get("status") in {400, 401, 403, 404, 409, 422}
                        record["status"] = "rejected" if definite else "uncertain"
                        if definite:
                            record["error"] = exc.payload["error"]
                        try:
                            hosted._write_private(path, record)
                        except OSError:
                            pass  # The durable pending fence still forbids a second POST.
                        if definite:
                            exc.payload["error"]["local_receipt"] = str(path)
                            raise
                        raise hosted.HostedError("QUESTION_OUTCOME_UNCERTAIN",
                            "Question delivery could not be confirmed. Use cloud question recover with this receipt; never blindly resend or change the request ID.", local_receipt=str(path)) from exc
                    result = _snapshot(record, path)
        print(json.dumps(hosted._redact(result, secrets), sort_keys=True, allow_nan=False))
        return 0
    except hosted.HostedError as exc:
        print(json.dumps(hosted._redact(exc.payload, secrets)), file=sys.stderr)
        return 1
    except (OSError, ValueError, UnicodeError, RecursionError):
        print(json.dumps({"error": {"code": "LOCAL_ERROR", "message": "Could not prepare or recover the company question. Keep any existing receipt; no local bookkeeping fallback was used."}}), file=sys.stderr)
        return 1
