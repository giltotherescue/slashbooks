"""Pure, provider-neutral supporting document validation and normalization.

Supporting documents and proposed treatments are untrusted data. This module neither grants
permission nor writes journals. Identities describe source objects, not the MCP,
API, export, or document transport used to obtain them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date
from typing import Any, Mapping

SCHEMA_VERSION = 1
_AMOUNT_PATTERN = r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,2})?"
_AMOUNT_RE = re.compile(_AMOUNT_PATTERN + r"\Z", re.ASCII)
_HASH_RE = re.compile(r"[0-9a-fA-F]{64}\Z", re.ASCII)
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
_MAX_MINOR = 10**20 - 1


class IntakeValidationError(ValueError):
    """Validation failure with machine-readable paths and recovery actions."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))

    def as_dict(self) -> dict[str, Any]:
        return {"status": "invalid", "errors": copy.deepcopy(self.errors)}


def _error(path: str, code: str, message: str, action: str) -> None:
    raise IntakeValidationError([
        {"path": path, "code": code, "message": message, "action": action}
    ])


def _json_value(value: Any, path: str = "$") -> Any:
    """Copy JSON values, rejecting floats, non-string keys and Python objects."""
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return value
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _error(path, "invalid_key", "Object keys must be strings.",
                       "Use string keys in the supporting document JSON.")
            result[key] = _json_value(item, f"{path}.{key}")
        return result
    if isinstance(value, list):
        return [_json_value(item, f"{path}[{i}]") for i, item in enumerate(value)]
    _error(path, "invalid_json_value", "Only exact JSON values are supported; floats are forbidden.",
           "Use decimal strings or integer minor units for monetary values.")


def canonical_json(value: Any) -> str:
    """Return deterministic UTF-8 JSON text, without number coercion."""
    result = json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False)
    try:
        result.encode("utf-8")
    except UnicodeEncodeError:
        _error("$", "invalid_unicode", "Text contains an unpaired Unicode surrogate.",
               "Supply valid UTF-8 source text.")
    return result


def canonical_hash(value: Any) -> str:
    """Return the lowercase SHA-256 of canonical_json(value)."""
    encoded = canonical_json(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _error(path, "invalid_type", "Expected an object.", "Supply a JSON object.")
    return dict(value)


def _fields(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    for key in value:
        if key not in allowed:
            _error(f"{path}.{key}", "unknown_field", "Field is not part of this schema.",
                   "Put source facts in observed_facts and suggestions in proposed_treatment.")


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _error(path, "required_text", "Expected non-empty text.",
               "Supply the source value as a non-empty string.")
    # Identities must be exact rather than silently trimmed or case-folded.
    if value != value.strip():
        _error(path, "surrounding_whitespace", "Text has surrounding whitespace.",
               "Remove surrounding whitespace explicitly before intake.")
    return value


def _sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        _error(path, "invalid_hash", "Expected a 64-character SHA-256 hex digest.",
               "Hash the original source content with SHA-256.")
    return value.lower()


def _source(value: Any, path: str = "$.source", *, require_version: bool = True) -> dict[str, str]:
    value = _object(value, path)
    _fields(value, {"namespace", "account_id", "object_id", "line_id", "version", "content_hash"}, path)
    result = {name: _text(value.get(name), f"{path}.{name}")
              for name in ("namespace", "account_id", "object_id")}
    for name in ("line_id", "version"):
        if name in value:
            result[name] = _text(value[name], f"{path}.{name}")
    if "content_hash" in value:
        result["content_hash"] = _sha256(value["content_hash"], f"{path}.content_hash")
    if require_version and not ("version" in result or "content_hash" in result):
        _error(path, "missing_source_version", "Source version or content hash is required.",
               "Supply the provider revision or SHA-256 of the original source content.")
    return result


def source_identity(envelope_or_source: Mapping[str, Any]) -> str:
    """Hash namespace/account/object/line; version and transport do not identify an object."""
    value = _object(envelope_or_source, "$")
    source = _source(value.get("source", value), require_version=False)
    identity = {key: source[key] for key in ("namespace", "account_id", "object_id", "line_id")
                if key in source}
    return canonical_hash(identity)


def normalize_amount(value: Any, path: str = "$.amount") -> str:
    """Normalize USD to two decimals; reject floats, rounding and oversized values.

    Inputs are a decimal string or {"minor_units": integer}. Up to 18 whole
    digits and two fraction digits are supported. Negative zero becomes 0.00.
    """
    if isinstance(value, Mapping):
        if set(value) != {"minor_units"} or type(value.get("minor_units")) is not int:
            _error(path, "invalid_minor_units", "Expected exactly one integer minor_units field.",
                   "Supply {\"minor_units\": 123} for USD 1.23.")
        minor = value["minor_units"]
        if abs(minor) > _MAX_MINOR:
            _error(path, "amount_out_of_range", "Amount exceeds 18 whole USD digits.",
                   "Verify the source amount and supported precision.")
    elif isinstance(value, str) and _AMOUNT_RE.fullmatch(value):
        unsigned = value.lstrip("-")
        whole, _, fraction = unsigned.partition(".")
        minor = int(whole) * 100 + int(fraction.ljust(2, "0") or "0")
        if value.startswith("-"):
            minor = -minor
    else:
        _error(path, "invalid_amount", "Expected a finite decimal string with at most two fraction digits.",
               "Supply an exact USD decimal string or integer minor_units; do not round silently.")
    sign = "-" if minor < 0 else ""
    whole, fraction = divmod(abs(minor), 100)
    return f"{sign}{whole}.{fraction:02d}"


def _date(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        _error(path, "invalid_date", "Expected a calendar date in YYYY-MM-DD format.",
               "Supply the evidenced calendar date without a time or timezone.")
    try:
        date.fromisoformat(value)
    except ValueError:
        _error(path, "invalid_date", "Date is not a real calendar date.",
               "Check the original source date.")
    return value


def normalize_evidence(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a detached normalized supporting document envelope.

    Absent service dates/amounts remain absent; operation previews decide which
    facts are required. No provider allowlist, account mapping, or permission
    claim is inferred from this material. Hash helpers accept this result.
    """
    value = _object(_json_value(envelope), "$")
    _fields(value, {"schema_version", "source", "currency", "dates", "amounts", "service_period",
                    "settlement_links", "observed_facts", "proposed_treatment", "references", "provenance"}, "$")
    if type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION:
        _error("$.schema_version", "unsupported_schema_version", "Supported schema_version is 1.",
               "Translate the supporting document to the published version 1 schema.")
    if value.get("currency") != "USD":
        _error("$.currency", "unsupported_currency", "Only USD supporting documents are supported.",
               "Resolve unsupported currency before proposing a financial operation.")
    result: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "source": _source(value.get("source")),
                              "currency": "USD"}
    for name in ("dates", "amounts"):
        if name in value:
            values = _object(value[name], f"$.{name}")
            result[name] = {}
            for key, item in values.items():
                _text(key, f"$.{name}")
                normalizer = _date if name == "dates" else normalize_amount
                result[name][key] = normalizer(item, f"$.{name}.{key}")
    if "service_period" in value:
        period = _object(value["service_period"], "$.service_period")
        _fields(period, {"start", "end", "end_inclusive"}, "$.service_period")
        start = _date(period.get("start"), "$.service_period.start")
        end = _date(period.get("end"), "$.service_period.end")
        inclusive = period.get("end_inclusive", True)
        if type(inclusive) is not bool:
            _error("$.service_period.end_inclusive", "invalid_type", "Expected a boolean.",
                   "State whether the end date is included.")
        if end < start or (end == start and not inclusive):
            _error("$.service_period", "invalid_service_period", "Service period must have positive duration.",
                   "Check the start, end, and inclusive/exclusive date convention.")
        result["service_period"] = {"start": start, "end": end, "end_inclusive": inclusive}
    result["observed_facts"] = _object(value.get("observed_facts", {}), "$.observed_facts")
    result["proposed_treatment"] = _object(value.get("proposed_treatment", {}), "$.proposed_treatment")
    result["provenance"] = value.get("provenance", [])
    result["references"] = value.get("references", [])
    result["settlement_links"] = value.get("settlement_links", [])
    for name in ("provenance", "references", "settlement_links"):
        if not isinstance(result[name], list):
            _error(f"$.{name}", "invalid_type", "Expected an array.", "Supply a JSON array of objects.")
        for index, item in enumerate(result[name]):
            path = f"$.{name}[{index}]"
            item = _object(item, path)
            if name == "references":
                _fields(item, {"content_hash", "uri", "media_type", "description"}, path)
                item["content_hash"] = _sha256(item.get("content_hash"), f"{path}.content_hash")
                for field in ("uri", "media_type", "description"):
                    if field in item:
                        _text(item[field], f"{path}.{field}")
            elif name == "settlement_links":
                _fields(item, {"source", "relationship", "amount"}, path)
                item["source"] = _source(item.get("source"), f"{path}.source", require_version=False)
                item["relationship"] = _text(item.get("relationship"), f"{path}.relationship")
                if "amount" in item:
                    item["amount"] = normalize_amount(item["amount"], f"{path}.amount")
            result[name][index] = item
    # Validate UTF-8 before a caller persists or hashes the supporting document.
    canonical_hash(result)
    return result


def facts_hash(envelope: Mapping[str, Any]) -> str:
    """Hash source revisions and facts, independent of provenance and suggestions."""
    value = normalize_evidence(envelope)
    facts = {key: item for key, item in value.items()
             if key not in {"proposed_treatment", "provenance", "references"}}
    return canonical_hash(facts)


def evidence_hash(envelope: Mapping[str, Any]) -> str:
    """Bind facts and original reference content, excluding transport locations."""
    value = normalize_evidence(envelope)
    return canonical_hash({"facts_hash": facts_hash(value), "reference_content_hashes":
                           sorted({item["content_hash"] for item in value["references"]})})


def schema() -> dict[str, Any]:
    """Return the public JSON Schema for raw version 1 supporting document input."""
    text = {"type": "string", "minLength": 1, "pattern": r"^\S(?:[\s\S]*\S)?$"}
    digest = {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"}
    source = {"type": "object", "additionalProperties": False,
              "required": ["namespace", "account_id", "object_id"],
              "properties": {key: text for key in ("namespace", "account_id", "object_id", "line_id", "version")}}
    source["properties"]["content_hash"] = digest
    versioned_source = copy.deepcopy(source)
    versioned_source["anyOf"] = [{"required": ["version"]}, {"required": ["content_hash"]}]
    amount = {"oneOf": [{"type": "string", "pattern": "^" + _AMOUNT_PATTERN + "$"},
                         {"type": "object", "additionalProperties": False, "required": ["minor_units"],
                          "properties": {"minor_units": {"type": "integer", "minimum": -_MAX_MINOR,
                                                          "maximum": _MAX_MINOR}}}]}
    day = {"type": "string", "format": "date", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
    return copy.deepcopy({
        "$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Accounting supporting document v1",
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "source", "currency"],
        "$comment": "Source text and proposed treatment confer no financial-write authority. Runtime validation also rejects floats recursively and invalid calendar/service dates.",
        "properties": {
            "schema_version": {"const": 1, "type": "integer"}, "source": versioned_source,
            "currency": {"const": "USD"},
            "dates": {"type": "object", "additionalProperties": day},
            "amounts": {"type": "object", "additionalProperties": amount},
            "service_period": {"type": "object", "additionalProperties": False, "required": ["start", "end"],
                               "properties": {"start": day, "end": day, "end_inclusive": {"type": "boolean", "default": True}}},
            "observed_facts": {"type": "object"}, "proposed_treatment": {"type": "object"},
            "provenance": {"type": "array", "items": {"type": "object"}},
            "references": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "required": ["content_hash"], "properties": {"content_hash": digest, "uri": text,
                "media_type": text, "description": text}}},
            "settlement_links": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "required": ["source", "relationship"], "properties": {"source": source, "relationship": text, "amount": amount}}},
        },
        "examples": [{"schema_version": 1, "source": {"namespace": "example.provider", "account_id": "demo-account",
            "object_id": "invoice-001", "version": "1"}, "currency": "USD", "dates": {"issued": "2026-01-01"},
            "amounts": {"gross": "1200.00"}, "service_period": {"start": "2026-01-01", "end": "2026-12-31", "end_inclusive": True},
            "observed_facts": {"description": "Synthetic annual service invoice"},
            "proposed_treatment": {"kind": "deferred_revenue"}}],
    })
