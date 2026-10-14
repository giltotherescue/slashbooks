"""Deterministic standing-policy decisions for trusted engine callers.

The caller authenticates the executor, fetches current authority independently
of the company workspace, and rechecks policy/revision at commit. This module
does not authenticate policy documents or authorize arbitrary CLI/file access.
Receipt text, questions, model confidence and learned context are evidence,
never inputs that can extend the authority in a StandingPolicy.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping, Sequence


PREPARATION_ACTIONS = frozenset({
    "import", "categorize", "split", "match_transfer", "correct", "adjust", "reconcile",
})
# Final approval, publication, lock/reopen and authority/configuration changes
# deliberately have no preparation permission, even if named in a document.
_PERIOD = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class PolicyError(ValueError):
    """Malformed authority or intent; no preparation decision may be inferred."""


class PreparationDenied(PermissionError):
    """An entire preflight batch was denied before any caller performs writes."""

    def __init__(self, index: int, reason: str):
        self.index = index
        self.reason = reason
        super().__init__(f"Preparation item {index} requires review: {reason}.")


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise PolicyError("Expected a bounded identifier.")
    return value


def _period(value: Any) -> str:
    if not isinstance(value, str) or not _PERIOD.fullmatch(value):
        raise PolicyError("Expected a calendar period.")
    date.fromisoformat(value + "-01")
    return value


def _identifiers(value: Any, limit: int = 1000) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise PolicyError("Expected a bounded identifier list.")
    result = tuple(sorted(_identifier(item) for item in value))
    if len(set(result)) != len(result):
        raise PolicyError("Duplicate identifiers are not permitted.")
    return result


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class StandingPolicy:
    policy_id: str
    version: int
    entity_key: str
    start_period: str
    end_period: str
    actions: tuple[str, ...]
    held_source_ids: tuple[str, ...]
    held_entry_ids: tuple[str, ...]

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> StandingPolicy:
        fields = {"schema_version", "policy_id", "version", "entity_key", "start_period", "end_period",
                  "actions", "held_source_ids", "held_entry_ids"}
        if not isinstance(document, Mapping) or set(document) != fields or type(document["schema_version"]) is not int or document["schema_version"] != 1:
            raise PolicyError("Expected the complete version-1 standing-policy contract.")
        if type(document["version"]) is not int or not 1 <= document["version"] <= 2**53 - 1:
            raise PolicyError("Expected a positive policy version.")
        actions = _identifiers(document["actions"], len(PREPARATION_ACTIONS))
        if not actions or not set(actions) <= PREPARATION_ACTIONS:
            raise PolicyError("Policy actions must be supported bookkeeping preparation operations.")
        start, end = _period(document["start_period"]), _period(document["end_period"])
        if start > end:
            raise PolicyError("Policy period bounds are reversed.")
        return cls(_identifier(document["policy_id"]), document["version"], _identifier(document["entity_key"]),
                   start, end, actions, _identifiers(document["held_source_ids"]), _identifiers(document["held_entry_ids"]))

    @property
    def fingerprint(self) -> str:
        return _fingerprint({"schema_version": 1, **asdict(self)})


@dataclass(frozen=True)
class PreparationIntent:
    action: str
    periods: tuple[str, ...]
    source_ids: tuple[str, ...]
    entry_ids: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    requires_human_review: bool = False

    def __post_init__(self) -> None:
        _identifier(self.action)
        # Immutable snapshots prevent changes between a batch preflight and its receipts.
        if not isinstance(self.periods, tuple) or not 1 <= len(self.periods) <= 1200:
            raise PolicyError("Every affected period, including the original correction period, is required.")
        if tuple(sorted(set(_period(period) for period in self.periods))) != self.periods:
            raise PolicyError("Affected periods must be unique and sorted.")
        for values in (self.source_ids, self.entry_ids):
            if not isinstance(values, tuple) or _identifiers(values) != values:
                raise PolicyError("Affected identifiers must be immutable, unique and sorted.")
        if not isinstance(self.evidence_hashes, tuple) or not 1 <= len(self.evidence_hashes) <= 1000 or any(
            not isinstance(value, str) or not _SHA256.fullmatch(value) for value in self.evidence_hashes
        ) or tuple(sorted(set(self.evidence_hashes))) != self.evidence_hashes:
            raise PolicyError("Verified evidence fingerprints are required.")
        if type(self.requires_human_review) is not bool:
            raise PolicyError("The review hold must be explicit.")


@dataclass(frozen=True)
class ExecutionContext:
    entity_key: str
    execution_id: str
    policy: StandingPolicy
    current_policy_version: int
    expected_revision: int
    current_revision: int
    expires_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.entity_key)
        _identifier(self.execution_id)
        if not isinstance(self.policy, StandingPolicy):
            raise PolicyError("Trusted standing policy is required.")
        # Validate direct dataclass construction as strictly as parsed policies.
        canonical = StandingPolicy.from_document({"schema_version": 1, **asdict(self.policy)})
        object.__setattr__(self, "policy", canonical)
        for revision in (self.current_policy_version, self.expected_revision, self.current_revision):
            if type(revision) is not int or not 0 <= revision <= 2**53 - 1:
                raise PolicyError("Versions and revisions must be nonnegative integers.")
        if not isinstance(self.expires_at, datetime) or self.expires_at.utcoffset() is None:
            raise PolicyError("A timezone-aware authority expiry is required.")


@dataclass(frozen=True)
class PreparationReceipt:
    rule: str
    policy_id: str
    policy_version: int
    policy_hash: str
    execution_id: str
    expected_revision: int
    intent_hash: str


def authorize_batch(context: ExecutionContext, intents: Sequence[PreparationIntent], *, now: datetime | None = None) -> tuple[PreparationReceipt, ...]:
    """All-or-nothing authority preflight, not a ledger transaction.

    Callers derive affected IDs, periods, holds and evidence hashes from their
    validated accounting inputs. Never accept the agent's assertions of these
    properties. Commit all returned decisions against the same revision, or
    reload and preflight again. No financial result is computed here.
    """
    if not isinstance(intents, (tuple, list)) or not 1 <= len(intents) <= 1000 or any(not isinstance(item, PreparationIntent) for item in intents):
        raise PolicyError("Expected a bounded batch of preparation intents.")
    intents = tuple(intents)
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise PolicyError("A timezone-aware evaluation time is required.")
    policy = context.policy
    if context.entity_key != policy.entity_key:
        raise PreparationDenied(0, "entity_mismatch")
    if context.current_policy_version != policy.version:
        raise PreparationDenied(0, "policy_changed")
    if context.expected_revision != context.current_revision:
        raise PreparationDenied(0, "revision_changed")
    if now >= context.expires_at:
        raise PreparationDenied(0, "authority_expired")
    held_sources, held_entries = set(policy.held_source_ids), set(policy.held_entry_ids)
    for index, intent in enumerate(intents):
        reason = None
        if intent.action not in policy.actions or intent.action not in PREPARATION_ACTIONS:
            reason = "action_not_authorized"
        elif any(period < policy.start_period or period > policy.end_period for period in intent.periods):
            reason = "period_outside_policy"
        elif intent.requires_human_review or held_sources.intersection(intent.source_ids) or held_entries.intersection(intent.entry_ids):
            reason = "explicit_review_hold"
        elif intent.action in {"import", "categorize", "split", "match_transfer"} and not intent.source_ids:
            reason = "source_lineage_required"
        elif intent.action == "correct" and not intent.entry_ids:
            reason = "original_entry_required"
        if reason:
            raise PreparationDenied(index, reason)
    policy_hash = policy.fingerprint
    return tuple(PreparationReceipt("standing_preparation_v1", policy.policy_id, policy.version, policy_hash,
                                    context.execution_id, context.expected_revision, _fingerprint(asdict(intent))) for intent in intents)
