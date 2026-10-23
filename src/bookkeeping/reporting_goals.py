"""What the owner wants the reports to show, recorded in ``entity.json``.

``entity.json`` key ``reporting_goals``::

    {"version": 1, "goals": ["profit_by_line", "cash_runway"], "lines": ["Installs", "Repairs"]}

``goals`` lists reporting goal codes (``GOAL_CODES``) in the owner's order. ``lines``
lists the service or product lines (or job kinds) the owner named, only with a line
or job goal. The engine stores and validates the goals. No report, entry or rule
changes because of them. An agent or a host reads them to decide what to propose.

``books entity set-reporting-goals`` writes the key (preview, then ``--apply``). It
writes the audit event ``reporting-goals-set``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

REPORTING_GOALS_KEY = "reporting_goals"
REPORTING_GOALS_VERSION = 1
GOAL_CODES = (
    "profit_by_line",
    "profit_by_job",
    "cash_runway",
    "spend_vs_last_year",
    "sales_tax_owed",
    "owner_pay",
    "recurring_revenue",
    "basics",
)
LINE_GOALS = ("profit_by_line", "profit_by_job")
MAX_GOALS = 8
MAX_LINES = 12
MAX_LINE_CHARS = 40
LINE_PUNCTUATION = frozenset(" &'.,/()+-")
SET_REPORTING_GOALS_KIND = "reporting_goals_set"
GOALS_NOTE = ("Records what the owner wants the reports to show. Slashbooks stores the goals only: "
              "no entry, category, rule or report changes because of them.")


class GoalsError(ValueError):
    """A reporting goals value is not valid. ``code`` names the rule."""

    def __init__(self, code: str, message: str, action: str = "Correct the value and run the preview again.") -> None:
        self.code, self.action = code, action
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"status": "blocked", "errors": [{"code": self.code, "message": str(self), "action": self.action}]}


def _line(value: Any) -> str:
    if not isinstance(value, str):
        raise GoalsError("invalid_line", "A line name must be text.")
    text = " ".join(value.split())
    if not 1 <= len(text) <= MAX_LINE_CHARS:
        raise GoalsError("invalid_line", f"A line name must have 1 to {MAX_LINE_CHARS} characters: {value!r}.")
    if any(not (char.isalnum() or char in LINE_PUNCTUATION) for char in text) or not any(char.isalnum() for char in text):
        raise GoalsError("invalid_line", f"A line name may use letters, digits, spaces and & ' . , / ( ) + - only: {value!r}.")
    return text


def validate(goals: Sequence[Any], lines: Sequence[Any] = ()) -> dict[str, Any]:
    """Return the stored form ``{"version": 1, "goals": [...], "lines": [...]}`` or raise ``GoalsError``.

    Goal codes come from ``GOAL_CODES``, with no repeats and at most ``MAX_GOALS``. Lines are allowed
    only with ``profit_by_line`` or ``profit_by_job``: at most ``MAX_LINES``, each 1 to ``MAX_LINE_CHARS``
    plain characters after spaces are collapsed, unique without regard to letter case.
    """
    goals, lines = list(goals), list(lines)
    if not goals:
        raise GoalsError("no_goals", "Give at least one goal.", "Use --goal CODE, or --clear to remove the goals.")
    unknown = [goal for goal in goals if goal not in GOAL_CODES]
    if unknown:
        raise GoalsError("unknown_goal", f"Unknown goal {unknown[0]!r}. Use one of {', '.join(GOAL_CODES)}.")
    if len(set(goals)) != len(goals):
        raise GoalsError("repeated_goal", "Give each goal one time.")
    if len(goals) > MAX_GOALS:
        raise GoalsError("too_many_goals", f"Give at most {MAX_GOALS} goals.")
    if lines and not set(goals) & set(LINE_GOALS):
        raise GoalsError("lines_need_line_goal", "Lines are allowed only with the goal profit_by_line or profit_by_job.")
    if len(lines) > MAX_LINES:
        raise GoalsError("too_many_lines", f"Give at most {MAX_LINES} lines.")
    cleaned = [_line(value) for value in lines]
    if len({value.casefold() for value in cleaned}) != len(cleaned):
        raise GoalsError("repeated_line", "Give each line one time (letter case does not make a new line).")
    return {"version": REPORTING_GOALS_VERSION, "goals": goals, "lines": cleaned}


def reporting_goals(config: Mapping[str, Any]) -> dict[str, Any] | None:
    """The recorded reporting goals, validated, or None when the company has none.

    Raises ``ValueError`` when ``entity.json`` holds a value that is not valid.
    """
    if REPORTING_GOALS_KEY not in config:
        return None
    value = config[REPORTING_GOALS_KEY]
    fix = "Run books entity set-reporting-goals to fix it."
    if (not isinstance(value, dict) or value.get("version") != REPORTING_GOALS_VERSION
            or set(value) - {"version", "goals", "lines"}
            or not isinstance(value.get("goals"), list) or not isinstance(value.get("lines", []), list)):
        raise ValueError(f"entity.json {REPORTING_GOALS_KEY} must be {{\"version\": 1, \"goals\": [...], \"lines\": [...]}}. {fix}")
    try:
        result = validate(value["goals"], value.get("lines", []))
    except GoalsError as exc:
        raise ValueError(f"entity.json {REPORTING_GOALS_KEY} is not valid: {exc} {fix}") from None
    if result["lines"] != value.get("lines", []):
        raise ValueError(f"entity.json {REPORTING_GOALS_KEY} has a line name with extra spaces. {fix}")
    return result


def preview_set(entity: Any, goals: Sequence[str] | None = None, lines: Sequence[str] | None = None,
                clear: bool = False) -> dict[str, Any]:
    """Preview recording (or with ``clear`` removing) the reporting goals. Writes nothing."""
    from .accrual.enable import _integrity
    from .accrual.intake import canonical_hash
    from .entity import load_entity
    from .ledger.store import LedgerStore, default_store_path
    entity = load_entity(entity.path)
    goals, lines = list(goals or []), list(lines or [])
    if clear:
        if goals or lines:
            raise GoalsError("clear_with_values", "Give --clear alone, or give --goal without --clear.")
        target = None
        request: dict[str, Any] = {"clear": True}
    else:
        if not goals and not lines:
            raise GoalsError("nothing_to_set", "Give --goal (and --line for a line or job goal), or --clear.",
                             "For example: books entity set-reporting-goals PATH --goal profit_by_line --line Installs --line Repairs")
        target = validate(goals, lines)
        request = {"goals": target["goals"], "lines": target["lines"]}
    config = entity.entity_config
    # Read the stored value without validating it, so this command can repair a bad value.
    current = config.get(REPORTING_GOALS_KEY)
    try:
        stored_valid = reporting_goals(config) is not None or current is None
    except ValueError:
        stored_valid = False
    _integrity(entity, "record the reporting goals")
    store = LedgerStore(default_store_path(entity.path))
    recorded, last_event = _last_goals_event(store)
    body: dict[str, Any] = {
        "schema_version": 1, "kind": SET_REPORTING_GOALS_KIND, "request": request,
        "changes": (REPORTING_GOALS_KEY in config) if target is None else current != target,
        "ledger_digest": store.content_digest(), "configuration_hash": canonical_hash(config),
        # The audit log is not in the ledger digest. The record hash of the last reporting-goals-set event
        # binds the plan to it, so a used plan hash cannot confirm a later change.
        "last_goals_event": last_event,
        "effects": {"from": current, "to": target, "entries_changed": 0, "accounts_changed": 0,
                    "period_locks_changed": 0, "published_statements_changed": False, "note": GOALS_NOTE}}
    warnings = []
    if not stored_valid:
        warnings.append({"code": "invalid_stored_value", "refs": [REPORTING_GOALS_KEY],
                         "message": f"entity.json holds a {REPORTING_GOALS_KEY} value that is not valid. This change replaces it."})
    if current != recorded:
        # Information only: entity.json is the state. A hand edit, or a stop between the file write and the
        # ledger commit, leaves a value that no reporting-goals-set event records. Nothing is written for it.
        warnings.append({"code": "unrecorded_change", "refs": [REPORTING_GOALS_KEY],
                         "message": f"entity.json holds a {REPORTING_GOALS_KEY} value that no reporting-goals-set "
                                    "audit event records. entity.json is the state; nothing is written for it."})
    if warnings:
        body["warnings"] = warnings
    body["request_hash"] = canonical_hash({"kind": SET_REPORTING_GOALS_KIND, "request": request})
    body["plan_hash"] = canonical_hash(body)
    return body


def recorded_goals(store: Any) -> Any:
    """The value of the last reporting-goals-set audit event, or None when there is none."""
    return _last_goals_event(store)[0]


def _last_goals_event(store: Any) -> tuple[Any, str]:
    """The value and the record hash of the last reporting-goals-set event; ``"none"`` when there is none."""
    events = [event for event in store.load_audit_events() if event["type"] == "reporting-goals-set"]
    if not events:
        return None, "none"
    return events[-1]["payload"].get("to"), events[-1]["record_hash"]


def apply_set(entity: Any, goals: Sequence[str] | None = None, lines: Sequence[str] | None = None,
              clear: bool = False, plan_hash: str | None = None) -> dict[str, Any]:
    """Record the reporting goals. Repeating the same values returns already_applied.

    entity.json is replaced inside the ledger transaction that writes the audit event. The event carries a
    unique ``operation_id``. If anything stops the apply after the file replace, a new database connection
    looks for that operation id. Only when it proves that the event is absent, and the file still holds the
    bytes this apply wrote, are the original bytes put back. Otherwise the new file stays.

    When entity.json already holds the requested value but no event records it (``unrecorded_change``),
    an apply with the plan hash of a fresh preview writes the event, marked ``confirmed_by_apply``.
    """
    import json
    import os
    import uuid
    from .accrual.enable import _seal
    from .entity import load_entity
    from .ledger.importer import _entity_write_lock
    from .ledger.store import LedgerStore, default_store_path
    # The company lock that every engine writer of entity.json takes, for the whole apply: read, stage,
    # rename, commit and recovery. Without the entity.json repair, so this command writes only its own change.
    with _entity_write_lock(entity.path, repair=False):
        fresh = preview_set(entity, goals, lines, clear)
        effects = fresh["effects"]
        store = LedgerStore(default_store_path(entity.path))
        unrecorded = "unrecorded_change" in {warning["code"] for warning in fresh.get("warnings", [])}
        if not fresh["changes"]:
            if not unrecorded or plan_hash is None:
                # An exact retry or a no-op. Resolve it before the plan hash check, which guards writes only.
                result = {"status": "already_applied", "kind": SET_REPORTING_GOALS_KIND,
                          REPORTING_GOALS_KEY: effects["to"], "request_hash": fresh["request_hash"]}
                if unrecorded:
                    result["warnings"] = fresh["warnings"]
                return result
            # entity.json holds the target, but no event records it. The plan hash of a fresh preview, which
            # shows unrecorded_change, authorizes writing the event for it.
            if plan_hash != fresh["plan_hash"]:
                raise GoalsError("stale_preview", "The plan hash does not match a preview of this state.",
                                 "Run the preview again and apply with its plan_hash.")
            operation_id = uuid.uuid4().hex
            with store.transaction(immediate=True) as conn:
                store.append_audit_event("reporting-goals-set", {"from": recorded_goals(store), "to": effects["to"],
                                                                 "plan_hash": fresh["plan_hash"], "operation_id": operation_id,
                                                                 "confirmed_by_apply": True}, conn)
                _seal(store, conn, "entity-set-reporting-goals")
            return {"status": "applied", "kind": SET_REPORTING_GOALS_KIND, REPORTING_GOALS_KEY: effects["to"],
                    "confirmed_by_apply": True, "operation_id": operation_id, "request_hash": fresh["request_hash"],
                    "plan_hash": fresh["plan_hash"], "entries_changed": 0, "published_statements_changed": False}
        if plan_hash is not None and plan_hash != fresh["plan_hash"]:
            raise GoalsError("stale_preview", "The company changed after the preview.", "Run the preview again.")
        entity = load_entity(entity.path)
        config = {key: value for key, value in entity.entity_config.items() if key != REPORTING_GOALS_KEY}
        if effects["to"] is not None:
            config[REPORTING_GOALS_KEY] = effects["to"]
        operation_id = uuid.uuid4().hex
        destination = entity.path / "entity.json"
        original = destination.read_bytes()
        written = (json.dumps(config, sort_keys=True, indent=2) + "\n").encode("utf-8")
        temp = entity.path / "entity.json.reporting-goals.tmp"
        replace_started = False
        try:
            _write_durable(temp, written)  # Inside the cleanup scope: a failure here leaves no temporary file.
            with store.transaction(immediate=True) as conn:
                store.append_audit_event("reporting-goals-set", {"from": effects["from"], "to": effects["to"],
                                                                 "plan_hash": fresh["plan_hash"],
                                                                 "operation_id": operation_id}, conn)
                _seal(store, conn, "entity-set-reporting-goals")
                # An editor that bypasses the company lock may have changed entity.json since the read. The
                # byte check narrows that window; it cannot close it, and such editors are not supported.
                if not _holds(destination, original):
                    raise GoalsError("stale_preview", "entity.json changed during the apply. Nothing was written.",
                                     "Run the preview again.")
                replace_started = True  # From here the destination may hold the new bytes, whatever stops us.
                _replace(temp, destination)
        except BaseException:
            if replace_started and _event_absent(store.path, operation_id) and _holds(destination, written):
                _write_durable(temp, original)  # The ledger rolled back: stage the original bytes.
                # Check again directly before the rename: another writer may have changed the file while the
                # rollback bytes were written and synced. A byte check cannot fully guard the rename itself
                # against an editor that does not take the company lock; that window stays very short.
                if _holds(destination, written):
                    os.replace(temp, destination)
                    try:
                        _fsync_directory(destination.parent)
                    except OSError:
                        pass  # The original bytes are in place; the first error is the one to report.
            raise
        finally:
            temp.unlink(missing_ok=True)
        return {"status": "applied", "kind": SET_REPORTING_GOALS_KIND, REPORTING_GOALS_KEY: effects["to"],
                "from": effects["from"], "operation_id": operation_id, "request_hash": fresh["request_hash"],
                "plan_hash": fresh["plan_hash"], "entries_changed": 0, "published_statements_changed": False}


def _write_durable(path: Path, data: bytes) -> None:
    import os
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _replace(source: Path, destination: Path) -> None:
    import os
    os.replace(source, destination)
    _fsync_directory(destination.parent)


def _fsync_directory(path: Path) -> None:
    import os
    if os.name == "posix":  # Make the rename durable; Windows has no directory handle to sync.
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _holds(path: Path, data: bytes) -> bool:
    """True when *path* still holds exactly *data*: no other writer changed it since."""
    try:
        return path.read_bytes() == data
    except OSError:
        return False


def _event_absent(store_path: Path, operation_id: str) -> bool:
    """True only when a NEW database connection proves that the event of *operation_id* is absent.

    A failed lookup returns False, so the new entity.json stays: entity.json is the state, and the next
    preview reports a value without an event as ``unrecorded_change``.
    """
    from .ledger.store import LedgerStore
    try:
        events = LedgerStore(store_path).load_audit_events()
    except Exception:
        return False
    return not any(event["type"] == "reporting-goals-set" and event["payload"].get("operation_id") == operation_id
                   for event in events)
