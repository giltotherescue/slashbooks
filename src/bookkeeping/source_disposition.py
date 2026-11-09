"""Pure evaluation of a person's decision to close one staged source row without booking it.

A bank row dated before an account's books start (or, with no books start policy, before the account's open
date) cannot be booked. A person may close such a row with one of two decisions:

- ``outside_books``: the row is not recorded in these books. This claims nothing about the opening balance.
- ``covered_by_opening``: the opening balance of the same account includes this row. This needs a match against
  a retained, immutable statement line. The engine keeps no such lines today, so this decision is always refused
  (``DISPOSITION_LINE_EVIDENCE_UNAVAILABLE``) and a stored claim of it never resolves. A date is never evidence.
  Refusing every such claim is deliberate unsupported behavior. It is not proof that any statement line was
  validated: the module validates no lines.

This module only evaluates. It stores nothing, writes nothing, reads no environment variable and no clock, and
decides no authority. A host decides who may confirm a decision, stores the confirmed claim, and calls
:func:`evaluate` again whenever the books change. This module never moves or changes a row. It does not verify a
row against a provider or an upload; ``row_digest`` only binds the staged bytes that a person confirmed.

Functions:

- :func:`row_identity`: the source ID, its spellings, and a digest of the source fields of a staged row.
- :func:`preview`: whether a row may be closed, the basis, the conflicts a person must see, and a claim.
- :func:`confirm`: accept a claim only when a fresh preview gives the same ``preview_digest``.
- :func:`evaluate`: ``resolved`` or ``invalidated`` (with trigger codes) for each stored claim.
- :func:`reclassify`: the two date checks of a restored row (not the other review and booking guards).
- :func:`captured_epoch`: the full epoch a confirmation captured. Only a value for a host to assert; no fence here.
- :func:`load_context`: read a company directory (read-only) into a context.
- ``python -I -m bookkeeping.source_disposition check --request R.json --entity DIR``: the same, as a command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .ledger.books_policy import (
    PENDING_OPENING,
    UNSET,
    BooksPolicy,
    BooksPolicyError,
    first_policy_conflict,
    parse_policy,
)
from .ledger.books_policy_check import entry_fingerprint
from .ledger.model import Entry, Posting
from .ledger.open_dates import opened_after
from .source_ids import source_id_variants

SCHEMA_VERSION = 1
STATES = ("outside_books", "covered_by_opening")
REASON_TEXT_LIMIT = 280

#: Fields that the importer adds to a staged row. They are derived again from the source fields, so they are not
#: part of the row identity. A test checks that every label the importer writes is in this set.
DERIVED_FIELDS = frozenset({"review_hold_reason", "accrual_review_required", "accrual_review_action"})

UNMAPPED_ACCOUNT = "Assets:Uncategorized"
OPENING_KINDS = {"statement-opening": "statement", "quickbooks-opening": "quickbooks"}
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class DispositionInputError(ValueError):
    """A context, claim or request does not have the documented shape."""

    code = "DISPOSITION_INPUT_INVALID"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _refused(code: str, reason: str, message: str, **detail: Any) -> dict[str, Any]:
    return {"status": "refused", "code": code, "reason": reason, "message": message, **detail}


def _strict_day(value: Any) -> date | None:
    """A ``YYYY-MM-DD`` day (a timestamp keeps its first ten characters), or ``None``. Never guesses a format."""
    if not isinstance(value, str):
        return None
    text = value[:10]
    if not _DAY.match(text) or (len(value) > 10 and value[10] not in "T "):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _amount(value: Any) -> str | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return format(number.quantize(Decimal("0.01")), "f") if number.is_finite() else None


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


_ENTRY_FIELDS = tuple(item.name for item in fields(Entry))
_POSTING_FIELDS = tuple(item.name for item in fields(Posting))


def _plain(value: Any) -> Any:
    """A model value as plain JSON, field by field. Nothing is left out."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if is_dataclass(value):
        return {item.name: _plain(getattr(value, item.name)) for item in fields(value)}
    return value


def entry_document(entry: Entry) -> dict[str, Any]:
    """Every field of ``entry`` and of each posting, as JSON. :func:`entry_from_document` reads it back exactly."""
    return _plain(entry)


def _pairs(value: Any, where: str) -> tuple[tuple[str, str], ...]:
    _require(isinstance(value, list) and all(isinstance(p, list) and len(p) == 2
                                             and all(isinstance(x, str) for x in p) for p in value),
             f"{where} must be a list of [text, text]")
    return tuple((p[0], p[1]) for p in value)


def _texts(value: Any, where: str) -> tuple[str, ...]:
    _require(isinstance(value, list) and all(isinstance(x, str) for x in value), f"{where} must be a list of text")
    return tuple(value)


def entry_from_document(document: Any) -> Entry:
    """Read an entry from :func:`entry_document` JSON. Every model field is required, and nothing else is allowed.

    A field added to ``Entry`` or ``Posting`` later makes this refuse every document until it is read here too.
    """
    _require(isinstance(document, Mapping) and set(document) == set(_ENTRY_FIELDS),
             f"an entry must have exactly the fields {sorted(_ENTRY_FIELDS)}")
    postings = document["postings"]
    _require(isinstance(postings, list), "entry postings must be a list")
    built = []
    for item in postings:
        _require(isinstance(item, Mapping) and set(item) == set(_POSTING_FIELDS),
                 f"a posting must have exactly the fields {sorted(_POSTING_FIELDS)}")
        _require(isinstance(item["account"], str) and isinstance(item["currency"], str)
                 and isinstance(item["amount"], str), "posting account, amount and currency must be text")
        try:
            amount = Decimal(item["amount"])
        except (InvalidOperation, ValueError):
            amount = Decimal("NaN")
        _require(amount.is_finite(), "posting amounts must be finite decimals")
        built.append((item["account"], amount, item["currency"], _pairs(item["meta"], "posting meta")))
    _require(all(isinstance(document[k], str) for k in ("narration", "flag"))
             and (document["payee"] is None or isinstance(document["payee"], str)), "bad entry text fields")
    try:
        return Entry(date=_day_field(document["date"], "entry date"), narration=document["narration"],
                     payee=document["payee"], flag=document["flag"], tags=_texts(document["tags"], "entry tags"),
                     links=_texts(document["links"], "entry links"), meta=_pairs(document["meta"], "entry meta"),
                     postings=tuple(Posting(a, amount, c, meta) for a, amount, c, meta in built))
    except DispositionInputError:
        raise
    except (TypeError, ValueError) as exc:
        raise DispositionInputError(f"The disposition input is not valid: bad entry ({exc})") from exc


# ``entry_fingerprint`` (imported above) is the SHA-256 of the #118 history key (``books_policy_check.entry_key``):
# every Entry and every Posting field. It is defined once, in ``books_policy_check``, with the same canonical JSON.


@dataclass(frozen=True)
class Opening:
    """One live opening entry, seen from one of its non-equity accounts. The whole entry is kept."""

    account: str
    entry: Entry

    @property
    def meta(self) -> dict[str, str]:
        return dict(self.entry.meta)

    @property
    def day(self) -> date:
        return self.entry.date

    @property
    def source_id(self) -> str:
        return self.meta.get("source-id", "")

    @property
    def kind(self) -> str:
        return OPENING_KINDS.get(self.meta.get("import-source", ""), "")

    @property
    def statement_id(self) -> str | None:
        return self.meta.get("statement-id")

    @property
    def amount(self) -> str:
        return format(sum((p.amount for p in self.entry.postings if p.account == self.account), Decimal("0")), "f")

    @property
    def fingerprint(self) -> str:
        return entry_fingerprint(self.entry)

    def document(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "date": self.day.isoformat(), "kind": self.kind,
                "statement_id": self.statement_id, "amount": self.amount, "entry_fingerprint": self.fingerprint}

    @property
    def digest(self) -> str:
        return _opening_digest(self.account, self.fingerprint)


def _opening_digest(account: str, fingerprint: str) -> str:
    return _sha({"account": account, "entry_fingerprint": fingerprint})


@dataclass(frozen=True)
class Statement:
    statement_id: str
    account: str
    period_start: date
    period_end: date
    evidence_sha256: str


@dataclass(frozen=True)
class Booked:
    source_id: str
    account: str
    day: date
    amount: str


@dataclass(frozen=True)
class DispositionContext:
    """Everything an evaluation reads. Build it with :func:`context_from_document` or :func:`load_context`."""

    rows: tuple[Mapping[str, Any], ...]
    bank_account_mappings: Mapping[str, Any]
    csv_account_mappings: Mapping[str, Any]
    open_dates: Mapping[str, date]
    openings: tuple[Opening, ...]
    statements: tuple[Statement, ...]
    booked: tuple[Booked, ...]
    policy: BooksPolicy | None


_CONTEXT_FIELDS = {"rows", "bank_account_mappings", "csv_account_mappings", "open_dates", "openings", "statements",
                   "booked", "policy"}


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise DispositionInputError(f"The disposition input is not valid: {detail}")


def _day_field(value: Any, where: str) -> date:
    day = _strict_day(value)
    _require(day is not None and len(str(value)) == 10, f"{where} must be a YYYY-MM-DD day")
    assert day is not None
    return day


def context_from_document(document: Mapping[str, Any]) -> DispositionContext:
    """Build a context from a plain JSON document. Unknown fields are refused."""
    _require(isinstance(document, Mapping), "the context must be an object")
    _require(set(document) <= _CONTEXT_FIELDS, f"unknown context fields {sorted(set(document) - _CONTEXT_FIELDS)}")
    rows = document.get("rows", [])
    _require(isinstance(rows, list) and all(isinstance(r, Mapping) for r in rows), "rows must be a list of objects")
    mappings = document.get("bank_account_mappings") or {}
    csv_mappings = document.get("csv_account_mappings") or {}
    _require(isinstance(mappings, Mapping) and isinstance(csv_mappings, Mapping), "mappings must be objects")
    raw_open = document.get("open_dates") or {}
    _require(isinstance(raw_open, Mapping), "open_dates must be an object")
    open_dates = {str(k): _day_field(v, f"open_dates[{k}]") for k, v in raw_open.items()}
    openings = []
    for item in document.get("openings") or []:
        _require(isinstance(item, Mapping) and set(item) == {"account", "entry"} and isinstance(item["account"], str),
                 "an opening must be {account, entry}")
        opening = Opening(item["account"], entry_from_document(item["entry"]))
        _require(bool(opening.source_id) and bool(opening.kind)
                 and any(p.account == opening.account for p in opening.entry.postings),
                 "an opening entry needs a source-id, a known import-source and a posting on its account")
        openings.append(opening)
    statements = []
    for item in document.get("statements") or []:
        _require(isinstance(item, Mapping), "bad statement")
        statements.append(Statement(str(item["statement_id"]), str(item["account"]),
                                    _day_field(item.get("period_start"), "period_start"),
                                    _day_field(item.get("period_end"), "period_end"),
                                    str(item.get("evidence_sha256") or "")))
    booked = []
    for item in document.get("booked") or []:
        _require(isinstance(item, Mapping), "bad booked row")
        booked.append(Booked(str(item["source_id"]), str(item.get("account") or ""),
                             _day_field(item.get("date"), "booked date"), _amount(item.get("amount")) or ""))
    policy_document = document.get("policy")
    policy = None if policy_document is None else parse_policy(policy_document)
    return DispositionContext(tuple(rows), dict(mappings), dict(csv_mappings), open_dates, tuple(openings),
                              tuple(statements), tuple(booked), policy)


def load_context(entity_path: Path | str, policy_document: Any = None) -> DispositionContext:
    """Read a company directory into a context. Nothing is written. The policy comes only from the caller."""
    from .coverage import load_statements
    from .ledger.store import LedgerStore, default_store_path

    root = Path(entity_path)
    config = json.loads((root / "entity.json").read_text(encoding="utf-8"))
    pending_path = root / "staging" / "pending-categorization.json"
    rows = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.is_file() else []
    open_dates: dict[str, str] = {}
    openings: list[dict[str, Any]] = []
    booked: list[dict[str, Any]] = []
    store_path = default_store_path(root)
    if store_path.is_file():
        store = LedgerStore(store_path)
        with store.connection() as conn:
            open_dates = {str(name): str(day)[:10] for name, day in conn.execute("SELECT name, open_date FROM accounts")}
        openings, booked = _ledger_facts(store.load_entries())
    records, _findings = load_statements(root)
    statements = [{"statement_id": r.statement_id, "account": r.account, "period_start": r.period_start.isoformat(),
                   "period_end": r.period_end.isoformat(), "evidence_sha256": r.evidence_sha256} for r in records
                  if r.kind == "statement"]
    return context_from_document({
        "rows": rows, "bank_account_mappings": config.get("bank_account_mappings") or {},
        "csv_account_mappings": config.get("csv_account_mappings") or {}, "open_dates": open_dates,
        "openings": openings, "statements": statements, "booked": booked, "policy": policy_document})


def _ledger_facts(entries: Sequence[Entry]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Live openings (complete entries) and booked source lines.

    Entries come from ``LedgerStore.load_entries``, so every entry and posting field is kept. A reversed entry and
    its reversal are left out.
    """
    reversed_ids = {dict(e.meta)["reverses"] for e in entries if "reverses" in dict(e.meta)}
    openings, booked = [], []
    for entry in entries:
        meta = dict(entry.meta)
        source_id = meta.get("source-id", "")
        if "reverses" in meta or not source_id or source_id in reversed_ids:
            continue
        if meta.get("import-source") in OPENING_KINDS:
            for account in dict.fromkeys(p.account for p in entry.postings if not p.account.startswith("Equity:")):
                openings.append({"account": account, "entry": entry_document(entry)})
            continue
        for posting in entry.postings:
            booked.append({"source_id": source_id, "account": posting.account, "date": entry.date.isoformat(),
                           "amount": str(posting.amount)})
    return openings, booked


# ---------------------------------------------------------------------------
# Row identity, account and eligibility
# ---------------------------------------------------------------------------


def row_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    """The source ID, every spelling of it, and the SHA-256 of the row's source fields (derived labels left out)."""
    source_id = str(row.get("id") or "")
    source = {key: value for key, value in row.items() if key not in DERIVED_FIELDS}
    return {"source_id": source_id, "spellings": sorted(source_id_variants(source_id)), "row_digest": _sha(source)}


def _find_row(context: DispositionContext, source_id: str) -> Mapping[str, Any] | None:
    wanted = source_id_variants(source_id)
    for row in context.rows:
        if source_id_variants(row.get("id")) & wanted:
            return row
    return None


def _booked_ids(context: DispositionContext) -> set[str]:
    ids: set[str] = set()
    for item in context.booked:
        ids |= source_id_variants(item.source_id)
    return ids


def _mapping(context: DispositionContext, row: Mapping[str, Any]) -> tuple[str | None, str]:
    """The ledger account of the row from the company's confirmed mapping, and the digest of that mapping entry."""
    from .ledger.importer import _ledger_account_for_txn

    key = row.get("csvMappingKey")
    entry = {"provider_account_id": row.get("accountId"), "csv_mapping_key": key,
             "bank_mapping": context.bank_account_mappings.get(str(row.get("accountId") or "")),
             "csv_mapping": context.csv_account_mappings.get(key) if isinstance(key, str) else None}
    if entry["bank_mapping"] is None and entry["csv_mapping"] is None:
        return None, _sha(entry)
    try:
        account = _ledger_account_for_txn(dict(row), dict(context.bank_account_mappings),
                                          dict(context.csv_account_mappings))
    except Exception:  # noqa: BLE001 - a mapping the importer refuses is not a mapping here either
        return None, _sha(entry)
    if not account or account == UNMAPPED_ACCOUNT:
        return None, _sha(entry)
    return account, _sha({**entry, "ledger_account": account})


def _policy_basis(context: DispositionContext, account: str) -> dict[str, Any]:
    policy = context.policy
    if policy is None or not policy.enforced:
        return {"mode": "legacy"}
    found = policy.accounts.get(account)
    basis: dict[str, Any] = {"mode": policy.mode, "epoch": policy.epoch,
                             "account_state": None if found is None else found.state}
    if found is not None and found.state != UNSET:
        basis.update(account_version=found.version,
                     start_day=None if found.start_day is None else found.start_day.isoformat())
    return basis


def _live_opening(context: DispositionContext, account: str) -> Opening | None:
    found = [item for item in context.openings if item.account == account]
    return max(found, key=lambda item: (item.day, item.source_id)) if found else None


@dataclass(frozen=True)
class _Eligibility:
    row: Mapping[str, Any]
    day: date | None
    account: str | None
    mapping_digest: str
    reason_code: str | None
    refusal: str | None


def _eligibility(context: DispositionContext, row: Mapping[str, Any]) -> _Eligibility:
    day = _strict_day(row.get("date"))
    account, mapping_digest = _mapping(context, row)

    def refused(reason: str) -> _Eligibility:
        return _Eligibility(row, day, account, mapping_digest, None, reason)

    if row.get("pending"):
        return refused("pending_row")
    if source_id_variants(row.get("id")) & _booked_ids(context):
        return refused("already_booked")
    if day is None:
        return refused("invalid_date")
    if account is None:
        return refused("account_not_mapped")
    policy = context.policy
    if policy is not None and policy.enforced:
        found = policy.accounts.get(account)
        if found is None:
            return refused("policy_required")
        if found.state == UNSET or found.start_day is None:
            return refused("start_unset")
        if day >= found.start_day:
            return refused("on_or_after_start")
        return _Eligibility(row, day, account, mapping_digest, "before_authorized_start", None)
    opened = context.open_dates.get(account)
    if opened is None:
        return refused("no_open_date")
    if day >= opened:
        return refused("on_or_after_open_date")
    return _Eligibility(row, day, account, mapping_digest, "before_open_date", None)


_REFUSAL_TEXT = {
    "pending_row": "The row is still pending at the bank.",
    "already_booked": "The row is already recorded in the books.",
    "invalid_date": "The row has no valid YYYY-MM-DD date.",
    "account_not_mapped": "The row's bank account is not mapped to an account in the books.",
    "policy_required": "The books start policy has no state for this account.",
    "start_unset": "This account has no books start yet.",
    "on_or_after_start": "The row is on or after the day the books start, so it can be recorded.",
    "no_open_date": "This account has no open date.",
    "on_or_after_open_date": "The row is on or after the account's open date, so it can be recorded.",
    "opening_not_committed": "The opening balance of this account is not recorded yet.",
}


def _conflicts(context: DispositionContext, ok: _Eligibility) -> list[str]:
    assert ok.account is not None and ok.day is not None
    found: set[str] = set()
    opening = _live_opening(context, ok.account)
    policy = _policy_basis(context, ok.account)
    start = policy.get("start_day")
    if opening is None:
        found.add("NO_OPENING")
    else:
        if opening.day > ok.day:
            found.add("OPENING_AFTER_ROW")
        if start is not None and opening.day.isoformat() != start:
            found.add("OPENING_DATE_NOT_START")
    opened = context.open_dates.get(ok.account)
    if start is not None and opened is not None and opened.isoformat() > start:
        # The catalog open date is separate from the books start and is usually earlier. Later is a conflict.
        found.add("OPEN_DATE_AFTER_START")
    if policy.get("account_state") == PENDING_OPENING:
        found.add("OPENING_NOT_COMMITTED")
    amount = _amount(ok.row.get("amount"))
    if any(item.account == ok.account and item.day == ok.day and item.amount == amount for item in context.booked):
        found.add("SIMILAR_ROW_BOOKED")
    return sorted(found)


# ---------------------------------------------------------------------------
# Coverage: refused until a retained-line contract exists
# ---------------------------------------------------------------------------

LINE_EVIDENCE_UNAVAILABLE = "DISPOSITION_LINE_EVIDENCE_UNAVAILABLE"


def _coverage(context: DispositionContext, ok: _Eligibility, evidence: Any) -> dict[str, Any]:
    """Always a refusal today. ``covered_by_opening`` needs proof that this exact transaction is in the opening.

    The proof must be a match against a retained, immutable statement line (the statement's file hash, the line's
    index or digest, its amount, date and account). The engine does not keep such lines: a statement import keeps
    the original file and a mutable record, and a staged row's ``statementId`` is a plain field that a later write
    can set. A field match, a statement period, a balance "as of" a day, or a date is not line membership. So every
    claim is refused, and only ``outside_books`` (which claims nothing about the opening) is offered.
    """
    if not isinstance(evidence, Mapping) or evidence.get("kind") not in ("statement_line", "source_history_handoff"):
        return _refused("DISPOSITION_EVIDENCE_REQUIRED", "evidence_required",
                        "Name the statement line that shows this row. A date alone is not evidence.")
    if evidence["kind"] == "source_history_handoff":
        return _refused("DISPOSITION_HANDOFF_UNAVAILABLE", "handoff_unavailable",
                        "A validated source-history handoff is not available yet.")
    return _refused(LINE_EVIDENCE_UNAVAILABLE, "no_retained_statement_lines",
                    "The books keep no fixed copy of statement lines, so this row cannot be matched to one. "
                    "Leave it out of the books instead.")


# ---------------------------------------------------------------------------
# Preview, confirm, evaluate, reclassify
# ---------------------------------------------------------------------------


def _reason_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > REASON_TEXT_LIMIT or any(ord(ch) < 32 or ord(ch) == 127
                                                                          for ch in value):
        raise DispositionInputError(
            f"The disposition input is not valid: reason_text must be plain text of at most {REASON_TEXT_LIMIT} "
            "characters on one line")
    return value.strip() or None


def preview(context: DispositionContext, source_id: str, state: str, *, reason_text: Any = None,
            evidence: Any = None) -> dict[str, Any]:
    """What a person must see before a decision, and the claim a host stores after the person confirms it."""
    try:
        text = _reason_text(reason_text)
    except DispositionInputError as exc:
        return _refused(exc.code, "input_invalid", str(exc))
    if state not in STATES:
        return _refused("DISPOSITION_INPUT_INVALID", "input_invalid", f"The state must be one of {list(STATES)}.")
    row = _find_row(context, str(source_id))
    if row is None:
        return _refused("DISPOSITION_ROW_NOT_FOUND", "row_not_found", "No staged row has that source ID.")
    ok = _eligibility(context, row)
    if ok.refusal is not None:
        return _refused("DISPOSITION_NOT_ELIGIBLE", ok.refusal, _REFUSAL_TEXT[ok.refusal])
    assert ok.account is not None and ok.day is not None
    policy = _policy_basis(context, ok.account)
    opening = _live_opening(context, ok.account)
    basis: dict[str, Any] = {
        "account": ok.account, "mapping_digest": ok.mapping_digest, "policy": policy,
        "catalog": {"open_date": None if ok.account not in context.open_dates
                    else context.open_dates[ok.account].isoformat()},
        "opening": None if opening is None else {**opening.document(), "digest": opening.digest},
        "evidence": None}
    if state == "covered_by_opening":
        if policy.get("account_state") == PENDING_OPENING:
            return _refused("DISPOSITION_NOT_ELIGIBLE", "opening_not_committed", _REFUSAL_TEXT["opening_not_committed"])
        # Refused in every case until a retained-line contract exists (see ``_coverage``).
        return _coverage(context, ok, evidence)
    identity = row_identity(row)
    conflicts = _conflicts(context, ok)
    body = {"schema_version": SCHEMA_VERSION, "source_id": identity["source_id"], "spellings": identity["spellings"],
            "row_digest": identity["row_digest"],
            "row": {"date": ok.day.isoformat(), "amount": _amount(row.get("amount")),
                    "currency": row.get("currency"), "provider_account_id": row.get("accountId"),
                    "description": row.get("description")},
            "state": state, "reason_code": ok.reason_code, "reason_text": text, "basis": basis,
            "conflicts": conflicts, "conflicts_digest": _sha(conflicts)}
    digest = _sha(body)
    claim = {key: body[key] for key in ("schema_version", "source_id", "row_digest", "state", "reason_code",
                                        "reason_text", "basis", "conflicts", "conflicts_digest")}
    claim["preview_digest"] = digest
    return {"status": "ok", **body, "preview_digest": digest, "claim": claim}


def _claim_evidence(claim: Mapping[str, Any]) -> Any:
    evidence = (claim.get("basis") or {}).get("evidence")
    return None if evidence is None else {"kind": evidence.get("kind"), "statement_id": evidence.get("statement_id")}


def _check_claim(claim: Any) -> None:
    _require(isinstance(claim, Mapping), "a claim must be an object")
    _require(claim.get("schema_version") == SCHEMA_VERSION, "unsupported claim schema_version")
    _require(isinstance(claim.get("source_id"), str) and claim["source_id"], "claim source_id is required")
    _require(claim.get("state") in STATES, "claim state is not valid")
    _require(isinstance(claim.get("row_digest"), str) and bool(_SHA.match(claim["row_digest"])),
             "claim row_digest must be a SHA-256")
    _require(isinstance(claim.get("basis"), Mapping), "claim basis is required")


def confirm(context: DispositionContext, claim: Mapping[str, Any]) -> dict[str, Any]:
    """Accept a claim only when the preview that the person confirmed is still the current preview."""
    _check_claim(claim)
    if not isinstance(claim.get("preview_digest"), str) or not _SHA.match(claim["preview_digest"]):
        return _refused("DISPOSITION_CONFIRMATION_REQUIRED", "confirmation_required",
                        "A person must confirm the preview of this decision.")
    current = preview(context, claim["source_id"], claim["state"], reason_text=claim.get("reason_text"),
                      evidence=_claim_evidence(claim))
    if current["status"] != "ok":
        return current
    if current["preview_digest"] != claim["preview_digest"]:
        return _refused("DISPOSITION_PREVIEW_CHANGED", "preview_changed",
                        "Something changed since the preview. Review the new preview.", preview=current)
    return {"status": "ok", "claim": current["claim"]}


def _expected_completion(context: DispositionContext, claim: Mapping[str, Any]) -> Opening | None:
    """The opening that completes the expected transition now, or ``None``.

    The claim is ``outside_books`` and was confirmed while the account was ``authorized_pending_opening`` with no
    opening. Now the same account is ``authorized`` with the same ``start_day``, its version is exactly one higher, and
    a live opening of that account is dated on that ``start_day``.
    """
    if claim.get("state") != "outside_books":
        return None
    basis = claim["basis"]
    old = basis.get("policy") or {}
    account = str(basis.get("account") or "")
    new = _policy_basis(context, account)
    opening = _live_opening(context, account)
    version = old.get("account_version")
    if (old.get("mode") == "enforced" and new.get("mode") == "enforced"
            and old.get("account_state") == PENDING_OPENING and new.get("account_state") == "authorized"
            and isinstance(version, int) and new.get("account_version") == version + 1
            and old.get("start_day") is not None and new.get("start_day") == old.get("start_day")
            and basis.get("opening") is None
            and opening is not None and opening.day.isoformat() == old.get("start_day")):
        return opening
    return None


_EVIDENCE_FIELDS = ("schema_version", "kind", "source_id", "claim_preview_digest", "account", "start_day",
                    "account_version", "opening")


def _transition_evidence(claim: Mapping[str, Any], opening: Opening, version: int) -> dict[str, Any]:
    """The evidence of one opening completion, which a host records one time for this claim."""
    body = {"schema_version": SCHEMA_VERSION, "kind": "expected_opening_recorded", "source_id": claim["source_id"],
            "claim_preview_digest": claim.get("preview_digest"), "account": claim["basis"].get("account"),
            "start_day": (claim["basis"].get("policy") or {}).get("start_day"), "account_version": version,
            "opening": {"source_id": opening.source_id, "date": opening.day.isoformat(),
                        "entry": entry_document(opening.entry), "digest": opening.digest}}
    return {**body, "evidence_digest": _sha(body)}


_OPENING_EVIDENCE_FIELDS = {"source_id", "date", "entry", "digest"}


def _evidence_fits(claim: Mapping[str, Any], evidence: Any) -> bool:
    """The evidence is well formed, intact, and was made for this exact claim and its expected version.

    Every nested field is checked: the opening must have exactly ``source_id``, ``date``, ``entry`` and ``digest``;
    the entry must read back as a complete ledger entry (finite amounts, every model field, nothing extra); and the
    digest must be the one that the entry's fingerprint gives.
    """
    if not isinstance(evidence, Mapping) or set(evidence) != {*_EVIDENCE_FIELDS, "evidence_digest"}:
        return False
    if evidence["schema_version"] != SCHEMA_VERSION or evidence["kind"] != "expected_opening_recorded":
        return False
    try:
        if evidence["evidence_digest"] != _sha({key: evidence[key] for key in _EVIDENCE_FIELDS}):
            return False
    except (TypeError, ValueError):
        return False
    opening = evidence["opening"]
    if (not isinstance(opening, Mapping) or set(opening) != _OPENING_EVIDENCE_FIELDS
            or not all(isinstance(opening[k], str) for k in ("source_id", "date", "digest"))):
        return False
    try:
        entry = entry_from_document(opening["entry"])
    except DispositionInputError:
        return False
    account = evidence["account"]
    if (not isinstance(account, str) or opening["digest"] != _opening_digest(account, entry_fingerprint(entry))
            or opening["source_id"] != dict(entry.meta).get("source-id") or opening["date"] != entry.date.isoformat()):
        return False
    old = claim["basis"].get("policy") or {}
    version = old.get("account_version")
    return (claim.get("state") == "outside_books" and old.get("account_state") == PENDING_OPENING
            and evidence["source_id"] == claim["source_id"]
            and evidence["claim_preview_digest"] == claim.get("preview_digest")
            and account == claim["basis"].get("account") and evidence["start_day"] == old.get("start_day")
            and isinstance(version, int) and type(evidence["account_version"]) is int
            and evidence["account_version"] == version + 1)


#: Conflicts that the expected opening transition removes (they described the pending state) and adds.
_PENDING_ONLY_CONFLICTS = frozenset({"NO_OPENING", "OPENING_NOT_COMMITTED"})
_EXPECTED_OPENING_CONFLICT = "OPENING_AFTER_ROW"


def _expected_conflicts(claim: Mapping[str, Any], transition: bool) -> str:
    """The conflict digest that the current conflicts must have."""
    if not transition:
        return str(claim.get("conflicts_digest"))
    old = claim.get("conflicts")
    _require(isinstance(old, list) and _sha(old) == claim.get("conflicts_digest"),
             "claim conflicts do not match conflicts_digest")
    return _sha(sorted((set(old) - _PENDING_ONLY_CONFLICTS) | {_EXPECTED_OPENING_CONFLICT}))


def captured_epoch(claim: Mapping[str, Any]) -> int | None:
    """The policy epoch that the person's confirmation captured, or ``None`` with no enforced policy.

    This is only a **value** for a host to assert (rule 2, separate from :func:`evaluate`). This module implements no
    fence and writes nothing. A host that publishes a confirmed decision must assert this full epoch in the same
    atomic write, so that a policy change on any account after the preview refuses the publication.
    """
    policy = (claim.get("basis") or {}).get("policy") or {}
    epoch = policy.get("epoch")
    return epoch if policy.get("mode") == "enforced" and isinstance(epoch, int) else None


def evaluate(context: DispositionContext, claims: Iterable[Mapping[str, Any]],
             transitions: Mapping[str, Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """For each stored claim: ``resolved``, ``invalidated`` (with trigger codes), or ``transition_pending``.

    A claim is resolved only when the row, its account mapping, the books start for that account (or the open
    date), the opening, the evidence and the conflicts are all the same as when the person confirmed it.

    Rule 1 (separate from the value :func:`captured_epoch`): a change of the policy epoch alone (for example, a start
    change on another account) changes nothing here.

    The expected opening transition (a pending opening is recorded under the same start, account and mapping) needs
    explicit evidence. ``transitions`` maps a claim's ``preview_digest`` to the evidence that the host recorded:

    - No evidence, and the transition shape holds now: ``transition_pending``, with the proposed
      ``transition_evidence`` for the opening in force now. It is not resolved. A host records that evidence one time
      (only when it has none for this claim) and evaluates again with it.
    - With evidence: later states are compared with the **recorded** completion: the account version, and the opening
      digest, which is the account plus :func:`entry_fingerprint` of the complete opening entry (every ``Entry`` and
      every ``Posting`` field, through the #118 history key). A reversed or replaced opening, or a change of any
      entry or posting field, gives ``OPENING_CHANGED``; another version or start gives ``POLICY_CHANGED``. Evidence
      that is malformed, edited, or does not fit the claim gives ``TRANSITION_EVIDENCE_INVALID``.

    The person's decision never changes. The transition never becomes coverage and never allows a booking.
    """
    results = []
    booked = _booked_ids(context)
    for claim in claims:
        _check_claim(claim)
        triggers: set[str] = set()
        notes: list[str] = []
        spellings = source_id_variants(claim["source_id"])
        row = _find_row(context, claim["source_id"])
        if spellings & booked:
            triggers.add("DISPOSED_ROW_BOOKED")
        if row is None or row_identity(row)["row_digest"] != claim["row_digest"]:
            triggers.add("SOURCE_ROW_CHANGED")
        basis = claim["basis"]
        if row is not None:
            ok = _eligibility(context, row)
            if ok.refusal is not None:
                triggers.add("NOT_ELIGIBLE_NOW")
            if ok.mapping_digest != basis.get("mapping_digest") or ok.account != basis.get("account"):
                triggers.add("MAPPING_CHANGED")
        account = str(basis.get("account") or "")
        opened = context.open_dates.get(account)
        if (basis.get("catalog") or {}).get("open_date") != (None if opened is None else opened.isoformat()):
            triggers.add("OPEN_DATE_CHANGED")
        new_policy = _policy_basis(context, account)
        new_policy.pop("epoch", None)
        opening = _live_opening(context, account)
        evidence = None if transitions is None else transitions.get(str(claim.get("preview_digest")))
        proposed: dict[str, Any] | None = None
        transition = False
        if evidence is not None:
            if not _evidence_fits(claim, evidence):
                triggers.add("TRANSITION_EVIDENCE_INVALID")
            else:
                recorded = {"mode": "enforced", "account_state": "authorized",
                            "account_version": evidence["account_version"], "start_day": evidence["start_day"]}
                if new_policy != recorded:
                    triggers.add("POLICY_CHANGED")
                if opening is None or opening.digest != evidence["opening"]["digest"]:
                    triggers.add("OPENING_CHANGED")
                transition = True
        else:
            completion = _expected_completion(context, claim)
            if completion is not None:
                proposed = _transition_evidence(claim, completion, int(new_policy["account_version"]))
                transition = True
            else:
                old_policy = dict(basis.get("policy") or {})
                old_policy.pop("epoch", None)
                if old_policy != new_policy:
                    triggers.add("POLICY_CHANGED")
                old_opening = basis.get("opening")
                if (None if opening is None else opening.digest) != (None if old_opening is None
                                                                      else old_opening.get("digest")):
                    triggers.add("OPENING_CHANGED")
        if claim["state"] == "covered_by_opening":
            # No coverage claim can be checked against a retained line today, so none is ever resolved.
            triggers.add("LINE_EVIDENCE_UNAVAILABLE")
        if row is not None and not triggers:
            ok = _eligibility(context, row)
            if _sha(_conflicts(context, ok)) != _expected_conflicts(claim, transition):
                triggers.add("CONFLICTS_CHANGED")
        if triggers:
            status, shown = "invalidated", None
        elif proposed is not None:
            status, shown = "transition_pending", proposed
        else:
            status, shown = "resolved", (dict(evidence) if evidence is not None else None)
            if evidence is not None:
                notes.append("EXPECTED_OPENING_RECORDED")
        results.append({"source_id": claim["source_id"], "state": claim["state"], "status": status,
                        "triggers": sorted(triggers), "notes": notes, "transition_evidence": shown})
    return results


def excluded_source_ids(results: Iterable[Mapping[str, Any]]) -> list[str]:
    """The rows that review and readiness may leave out: only ``resolved`` ones. Never counted as reconciled."""
    return sorted(str(item["source_id"]) for item in results if item.get("status") == "resolved")


#: Guards that :func:`reclassify` does not run. A host must run the full ordinary review and booking guards.
NOT_CHECKED_BY_RECLASSIFY = ("accrual_review", "ask_first_amount", "ordinary_review_and_booking")


def reclassify(context: DispositionContext, source_id: str, expected_row_digest: str) -> dict[str, Any]:
    """The two date checks for a restored row, under the current open dates and books start policy.

    It reads the row that is in the queue now and requires the same ``row_digest`` as the confirmed claim. That
    only shows that the staged bytes are the confirmed ones; it does not verify them against a provider or an upload.

    ``date_checks_pass`` is true only when neither date check refuses. It does **not** mean that the row may be
    booked: the ask-first amount, the accrual review and every other review and booking guard are not run here
    (``not_checked``). The result has no ``review_hold_reason``, so a caller cannot clear the row's other labels
    with it.
    """
    row = _find_row(context, str(source_id))
    if row is None or row_identity(row)["row_digest"] != expected_row_digest:
        return _refused("SOURCE_ROW_CHANGED", "source_row_changed",
                        "The row changed or is gone since the decision. A person must review it.")
    result: dict[str, Any] = {"status": "ok", "source_id": str(row.get("id")),
                              "not_checked": list(NOT_CHECKED_BY_RECLASSIFY)}
    day = _strict_day(row.get("date"))
    account, _digest = _mapping(context, row)
    if day is None or account is None:
        return {**result, "date_hold_reason": None, "date_checks_pass": False,
                "reason": "invalid_date" if day is None else "account_not_mapped"}
    hold: str | None = "source_catalog_conflict" if opened_after(context.open_dates, account, day) else None
    if hold is None:
        refusal = first_policy_conflict(context.policy, [(day, [account], str(row.get("id")))], [account])
        hold = None if refusal is None else refusal.code
    return {**result, "date_hold_reason": hold, "date_checks_pass": hold is None}


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

_REQUEST_FIELDS = {
    "preview": {"schema_version", "action", "policy", "source_id", "state", "reason_text", "evidence"},
    "confirm": {"schema_version", "action", "policy", "claim"},
    "evaluate": {"schema_version", "action", "policy", "claims", "transitions"},
    "reclassify": {"schema_version", "action", "policy", "source_id", "row_digest"},
}


def run_request(request: Mapping[str, Any], context: DispositionContext) -> dict[str, Any]:
    """Run one request on a context. ``policy`` in the request is applied by the caller (see :func:`main`)."""
    action = request.get("action")
    if action == "preview":
        return preview(context, str(request.get("source_id") or ""), str(request.get("state") or ""),
                       reason_text=request.get("reason_text"), evidence=request.get("evidence"))
    if action == "confirm":
        return confirm(context, request["claim"])
    if action == "evaluate":
        claims = request.get("claims")
        _require(isinstance(claims, list), "claims must be a list")
        transitions = request.get("transitions")
        _require(transitions is None or isinstance(transitions, Mapping), "transitions must be an object")
        results = evaluate(context, claims, transitions)
        status = "ok" if all(item["status"] == "resolved" for item in results) else "invalidated"
        return {"status": status, "results": results, "excluded_source_ids": excluded_source_ids(results)}
    return reclassify(context, str(request.get("source_id") or ""), str(request.get("row_digest") or ""))


def _parse_request(document: Any) -> Mapping[str, Any]:
    _require(isinstance(document, Mapping), "the request must be an object")
    _require(document.get("schema_version") == SCHEMA_VERSION, "unsupported schema_version")
    action = document.get("action")
    _require(action in _REQUEST_FIELDS, f"action must be one of {sorted(_REQUEST_FIELDS)}")
    _require(set(document) <= _REQUEST_FIELDS[action] and "policy" in document,
             f"unknown or missing request fields for {action}")
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -I -m bookkeeping.source_disposition")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Evaluate a disposition request against a company directory (read-only).")
    check.add_argument("--request", type=Path, required=True)
    check.add_argument("--entity", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        request = _parse_request(json.loads(args.request.read_text(encoding="utf-8"),
                                            parse_constant=_reject_constant))
        result = run_request(request, load_context(args.entity, request["policy"]))
    except (DispositionInputError, BooksPolicyError) as exc:
        code = getattr(exc, "code", DispositionInputError.code)
        print(json.dumps({"status": "error", "error": {"code": code, "message": str(exc)}}))
        return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "error": {"code": DispositionInputError.code,
                                                       "message": f"{type(exc).__name__}: {exc}"}}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


def _reject_constant(name: str) -> Any:
    raise DispositionInputError(f"The disposition input is not valid: {name} is not allowed")


if __name__ == "__main__":
    raise SystemExit(main())
