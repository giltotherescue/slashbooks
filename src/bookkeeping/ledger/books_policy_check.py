"""Trusted check of a proposed ledger change against a host's books start policy.

The check inside a command (``SLASHBOOKS_BOOKS_POLICY``, ``ledger.books_policy``) gives early feedback, but
it is not the fence. A session that runs the engine can remove the variable or the file, drop accounts from
the document, or set ``mode`` to ``legacy``. The host's policy epoch does not change in any of these cases,
so an epoch check at publication does not find them.

This module is the fence. The host runs it in a process that the agent cannot configure, after the engine
command and before publication:

    python -I -m bookkeeping.ledger.books_policy_check check --request REQUEST.json \\
        --base BASE_ENTITY_DIR --candidate CANDIDATE_ENTITY_DIR

* ``REQUEST.json`` comes from the host's immutable job snapshot, never from the workspace. Use request version
  2 (``FULL_ENTRY_REQUEST_SCHEMA_VERSION``)::

      {"schema_version": 2,
       "policy": {<the books start policy document, ledger.books_policy>},
       "scope": ["Assets:Bank:Checking", "Liabilities:Card-1234"],
       "opening_authorizations": [
         {"account": "Assets:Bank:Checking", "start_day": "2026-03-15", "version": 1,
          "operation": "quickbooks_sync_opening", "operation_id": "<host receipt ID>",
          "entry_source_id": "<source ID of the reviewed opening entry>",
          "entry_fingerprint": "<proposed_entry_fingerprint of the reviewed preview>"}]}

  ``entry_fingerprint`` is :func:`entry_fingerprint` of the complete opening entry: 64 lowercase hexadecimal
  characters. A version 2 request without it, with another form of it, or with the version 1 ``postings`` field
  is invalid. Version 2 has no fallback to version 1. A version 2 request that contains a non-finite number
  anywhere is invalid.

  Request version 1 (``REQUEST_SCHEMA_VERSION``) is the released version. It is still read with its released
  meaning, but it is WEAKER: its authorization has ``postings`` (``[["Assets:Bank:Checking", "100.00", "USD"],
  ...]``, with the posting metadata as an optional fourth item) in place of ``entry_fingerprint``, so it binds
  the entry date, the source ID and the postings only. It does not bind the narration, the payee, the flag, the
  tags, the links or the entry metadata. Do not use version 1 for a new host.

  Request version 3 (``PRIOR_DAY_REQUEST_SCHEMA_VERSION``) has one operation only, ``accrual_opening_prior_day``:
  a fresh accrual opening, which the native accrual opening posts on the day before the cutover. Its rows have
  the version 2 fields. ``start_day`` is the reviewed cutover; the engine derives the entry date (``start_day``
  minus one day), and a request cannot give another date. There is one row for each protected account that the
  entry posts to. Each row has its own ``operation_id`` (a reviewed authorization ID; two rows with one ID make the
  request invalid). Every row has the same ``start_day``, the same ``entry_source_id`` (the native
  ``accounting_plan.id``, which is the entry's ``source-id``) and the same ``entry_fingerprint``. See
  :func:`_prior_day_group` for the complete rules. The other request versions do not read this operation, and
  version 3 reads no other operation.

* ``BASE_ENTITY_DIR`` is the workspace that the host published before the job. ``CANDIDATE_ENTITY_DIR`` is
  the workspace that the job wants to publish.

The check never reads ``SLASHBOOKS_BOOKS_POLICY`` or any other environment variable. It compares the two
ledgers. Every base entry must still be in the candidate (``BOOKS_LEDGER_HISTORY_CHANGED``). Every entry that
the candidate adds is then checked line by line with the policy rules of ``ledger.books_policy``. The scope
includes the request ``scope``, every account that the policy names, and two kinds of evidence from either
workspace: bank or CSV mapping targets, and catalog accounts explicitly classified as bank, credit card or cash.
The catalog can put an unmapped account in scope. Only the host's policy can authorize its start.

The ledger is compared as a multiset of entries, so the order of entries does not matter. An entry is compared
on every ``Entry`` field and every ``Posting`` field, posting metadata included. The order of the postings
inside an entry is part of the entry, so a change of that order is an edit. Stored data outside the entry model
(row IDs, the derived ``session`` and ``late_arrival`` columns, the account catalog, the audit log) is not
compared here; the host checks the store itself.

Only two things let an added line through a refusal:

* The added entry is the exact reversal of a base entry: the same date, the same postings with each amount
  negated. The original must be the only base entry with that source ID (in any spelling), no base entry may
  reverse it already, and no other added entry may claim it. If two added entries claim one original, neither
  is exempt. A reversal of an added entry is never exempt, so two new entries cannot exempt each other.
* The account is ``authorized_pending_opening`` and an opening authorization from the host matches the entry:
  the account, ``start_day`` and ``version`` of the policy entry, the entry date and the entry source ID. In
  request version 2 the entry must also have the authorized ``entry_fingerprint``, so every ``Entry`` field and
  every ``Posting`` field is bound. In request version 1 (weaker) only every posting of the entry is bound
  (account, amount, currency and posting metadata). Two authorizations for one account, start day and version
  make the request invalid. An authorization that matches more than one added entry matches none. The host
  derives an authorization only from committed evidence of a reviewed opening operation (its own receipt) and
  the preview that a person reviewed (``proposed_entry`` and ``proposed_entry_fingerprint``). Entry metadata such
  as ``import-source``, a source ID alone or ``reverses`` never counts.

``check_workspaces`` copies both workspaces to a private folder first, and checks and digests the copies. It
never changes its inputs. Its result has ``request_sha256`` (``request_digest``), ``base_sha256`` and
``candidate_sha256`` (``workspace_digest``), and ``package_version``. The package version is a label, not a
build identity. ``build_receipt`` formats a receipt and refuses one without a full ``engine_build`` commit. It
is a formatting helper, not attestation: any Python code can call it with any values. That ``engine_build`` and
every other receipt field come from a trusted origin, and that the receipt reaches the host through a trusted
channel, are host requirements only. This module does not provide them.

Exit code 0 means ``ok``, 1 means ``refused`` (nothing may be published), 2 means a bad request or workspace.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .books_policy import OPENING_REQUIRED, PENDING_OPENING, BooksPolicy, BooksPolicyError, _invalid, parse_policy
from .model import Entry

#: The released request version. WEAKER: an opening authorization binds the date, the source ID and the postings.
REQUEST_SCHEMA_VERSION = 1
#: The request version that binds an opening authorization to ``entry_fingerprint`` of the complete entry.
FULL_ENTRY_REQUEST_SCHEMA_VERSION = 2
#: The request version of a fresh accrual opening on the day before the cutover (one operation only).
PRIOR_DAY_REQUEST_SCHEMA_VERSION = 3
REQUEST_SCHEMA_VERSIONS = (REQUEST_SCHEMA_VERSION, FULL_ENTRY_REQUEST_SCHEMA_VERSION,
                           PRIOR_DAY_REQUEST_SCHEMA_VERSION)
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
HISTORY_CHANGED = "BOOKS_LEDGER_HISTORY_CHANGED"
OPENING_OPERATIONS = ("quickbooks_sync_opening", "quickbooks_import_opening", "statement_opening",
                      "starting_balance", "accrual_opening")
#: The only operation of request version 3. Request versions 1 and 2 do not read it.
PRIOR_DAY_OPERATION = "accrual_opening_prior_day"
#: A version 3 opening group that does not match the one added entry exactly.
OPENING_AUTHORIZATION_MISMATCH = "BOOKS_OPENING_AUTHORIZATION_MISMATCH"
#: The complete entry metadata of a fresh accrual opening (``accrual.migration._effects``), with no tracking or
#: summary keys.
_PRIOR_DAY_META_KEYS = frozenset({"source-id", "import-source", "accrual-opening", "coverage-id", "cutover-date"})
_DAY_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class OpeningAuthorization:
    account: str
    start_day: date
    version: int
    operation: str
    operation_id: str
    entry_source_id: str
    postings: tuple[tuple[str, Decimal, str, tuple[tuple[str, str], ...]], ...]  # version 1 only
    entry_fingerprint: str | None = None  # version 2 only


@dataclass(frozen=True)
class CheckRequest:
    policy: BooksPolicy
    scope: frozenset[str]
    openings: tuple[OpeningAuthorization, ...]
    schema_version: int = REQUEST_SCHEMA_VERSION


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{where} must be non-empty text")
    return value


def _authorization(raw: Any, index: int, schema_version: int = REQUEST_SCHEMA_VERSION) -> OpeningAuthorization:
    where = f"opening_authorizations[{index}]"
    full = schema_version in (FULL_ENTRY_REQUEST_SCHEMA_VERSION, PRIOR_DAY_REQUEST_SCHEMA_VERSION)
    fields = {"account", "start_day", "version", "operation", "operation_id", "entry_source_id",
              "entry_fingerprint" if full else "postings"}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise _invalid(f"{where} must have exactly the fields {', '.join(sorted(fields))}")
    text = _text(raw["start_day"], f"{where}.start_day")
    try:
        start = date.fromisoformat(text)
    except ValueError:
        raise _invalid(f"{where}.start_day must be a YYYY-MM-DD date") from None
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise _invalid(f"{where}.version must be an integer of 1 or more")
    if schema_version == PRIOR_DAY_REQUEST_SCHEMA_VERSION:
        if raw["operation"] != PRIOR_DAY_OPERATION:
            raise _invalid(f"{where}.operation: a schema_version {PRIOR_DAY_REQUEST_SCHEMA_VERSION} check request "
                           f"reads only the operation {PRIOR_DAY_OPERATION}")
        if not _DAY_TEXT.fullmatch(text) or start.isoformat() != text:
            raise _invalid(f"{where}.start_day must be exact YYYY-MM-DD text")
        if start == date.min:
            raise _invalid(f"{where}.start_day has no day before it")
    elif raw["operation"] not in OPENING_OPERATIONS:
        raise _invalid(f"{where}.operation must be one of {', '.join(OPENING_OPERATIONS)}")
    if full:
        fingerprint = raw["entry_fingerprint"]
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            raise _invalid(f"{where}.entry_fingerprint must be the 64 lowercase hexadecimal characters of "
                           "the entry_fingerprint of the reviewed entry")
        return OpeningAuthorization(_text(raw["account"], f"{where}.account"), start, version, raw["operation"],
                                    _text(raw["operation_id"], f"{where}.operation_id"),
                                    _text(raw["entry_source_id"], f"{where}.entry_source_id"), (), fingerprint)
    postings = raw["postings"]
    if not isinstance(postings, list) or len(postings) < 2:
        raise _invalid(f"{where}.postings must list every posting of the opening entry")
    parsed = []
    for item in postings:
        if not isinstance(item, list) or len(item) not in (3, 4) or not isinstance(item[1], str):
            raise _invalid(f"{where}.postings items must be [account, \"amount\", currency] or "
                           "[account, \"amount\", currency, {posting metadata}]")
        meta = item[3] if len(item) == 4 else {}
        if not isinstance(meta, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in meta.items()):
            raise _invalid(f"{where}.postings metadata must be an object of text values")
        try:
            amount = Decimal(item[1])
        except InvalidOperation:
            raise _invalid(f"{where}.postings amounts must be decimal text") from None
        if not amount.is_finite():
            raise _invalid(f"{where}.postings amounts must be finite decimal text")
        parsed.append((_text(item[0], f"{where}.postings account"), amount,
                       _text(item[2], f"{where}.postings currency"), tuple(sorted(meta.items()))))
    return OpeningAuthorization(_text(raw["account"], f"{where}.account"), start, version, raw["operation"],
                                _text(raw["operation_id"], f"{where}.operation_id"),
                                _text(raw["entry_source_id"], f"{where}.entry_source_id"), tuple(parsed))


def parse_request(document: Any) -> CheckRequest:
    """Validate a decoded check request. Raises ``BooksPolicyError`` (``BOOKS_POLICY_INVALID``)."""
    if not isinstance(document, dict) or set(document) - {"schema_version", "policy", "scope",
                                                          "opening_authorizations"}:
        raise _invalid("the check request must be an object with schema_version, policy, scope and "
                       "opening_authorizations")
    raw_version = document.get("schema_version")
    if type(raw_version) is int and raw_version == FULL_ENTRY_REQUEST_SCHEMA_VERSION:
        schema_version = FULL_ENTRY_REQUEST_SCHEMA_VERSION
    elif type(raw_version) is int and raw_version == PRIOR_DAY_REQUEST_SCHEMA_VERSION:
        schema_version = PRIOR_DAY_REQUEST_SCHEMA_VERSION
    elif raw_version == REQUEST_SCHEMA_VERSION and not isinstance(raw_version, bool):
        schema_version = REQUEST_SCHEMA_VERSION  # the released test, unchanged
    else:
        raise _invalid(f"the check request schema_version must be {FULL_ENTRY_REQUEST_SCHEMA_VERSION} "
                       f"(or {REQUEST_SCHEMA_VERSION}, the weaker released version)")
    if schema_version != REQUEST_SCHEMA_VERSION:
        try:
            json.dumps(document, allow_nan=False)
        except (TypeError, ValueError):
            raise _invalid(f"a version {schema_version} check request must be JSON data with no non-finite "
                           "number") from None
    if "policy" not in document or "scope" not in document:
        raise _invalid("the check request needs policy and scope")
    scope = document["scope"]
    if not isinstance(scope, list):
        raise _invalid("scope must be a list of ledger accounts")
    raw_openings = document.get("opening_authorizations", [])
    if not isinstance(raw_openings, list):
        raise _invalid("opening_authorizations must be a list")
    openings = tuple(_authorization(item, index, schema_version) for index, item in enumerate(raw_openings))
    keys = Counter((auth.account, auth.start_day, auth.version) for auth in openings)
    twice = sorted(key for key, count in keys.items() if count > 1)
    if twice:
        raise _invalid(f"more than one authorization names {twice[0][0]} on {twice[0][1].isoformat()} "
                       f"version {twice[0][2]}")
    policy = parse_policy(document["policy"])
    if schema_version == PRIOR_DAY_REQUEST_SCHEMA_VERSION:
        _check_prior_day_rows(openings, policy)
    return CheckRequest(policy, frozenset(_text(a, "scope item") for a in scope), openings, schema_version)


def _check_prior_day_rows(openings: Sequence[OpeningAuthorization], policy: BooksPolicy) -> None:
    """The request-level rules of a version 3 group: one entry, one cutover, one row and one ID per account."""
    if not policy.enforced:
        raise _invalid(f"a schema_version {PRIOR_DAY_REQUEST_SCHEMA_VERSION} check request needs an enforced policy")
    if not openings:
        raise _invalid(f"a schema_version {PRIOR_DAY_REQUEST_SCHEMA_VERSION} check request needs its opening "
                       "authorizations")
    if len({(auth.start_day, auth.entry_source_id, auth.entry_fingerprint) for auth in openings}) != 1:
        raise _invalid("every version 3 authorization must have the same start_day, entry_source_id and "
                       "entry_fingerprint")
    if len({auth.account for auth in openings}) != len(openings):
        raise _invalid("a version 3 request has one authorization for each account")
    if len({auth.operation_id for auth in openings}) != len(openings):
        raise _invalid("each version 3 authorization needs its own operation_id")


def _field_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return ("decimal", str(value))  # "100.0" and "100.00" are different stored values
    if isinstance(value, date):
        return ("date", value.isoformat())
    if isinstance(value, tuple):
        return tuple(_field_value(item) for item in value)
    if is_dataclass(value):
        return _record(value)
    return value


def _record(value: Any) -> tuple:
    """Every dataclass field of ``value``, by name. A field added to ``Entry`` or ``Posting`` later is included."""
    return tuple((item.name, _field_value(getattr(value, item.name))) for item in fields(value))


def _key(entry: Entry) -> tuple:
    """The identity of an entry for the history comparison: every ``Entry`` field and every ``Posting`` field."""
    return _record(entry)


def entry_key(entry: Entry) -> tuple:
    """Public form of the history key: every ``Entry`` field and every ``Posting`` field, by name.

    Other modules use this one key, so a field added to the model later is included everywhere.
    """
    return _key(entry)


def entry_fingerprint(entry: Entry) -> str:
    """SHA-256 of the canonical JSON of :func:`entry_key`: every ``Entry`` field and every ``Posting`` field.

    The canonical JSON has sorted keys, no spaces and UTF-8 text. This is the one entry fingerprint: the opening
    previews print it (``proposed_entry_fingerprint``), a version 2 check request binds an opening to it, and
    ``source_disposition`` uses it.
    """
    text = json.dumps(_key(entry), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reviewed_entry(entry: Entry) -> dict[str, Any]:
    """What an opening preview prints, so that a host can bind a person's review to the complete entry.

    ``proposed_entry`` is ``source_disposition.entry_document`` (every model field; read it back with the strict
    ``source_disposition.entry_from_document``). ``proposed_entry_fingerprint`` is :func:`entry_fingerprint`.
    """
    from ..source_disposition import entry_document

    return {"proposed_entry": entry_document(entry), "proposed_entry_fingerprint": entry_fingerprint(entry)}


def ledger_delta(base: Sequence[Entry], candidate: Sequence[Entry]) -> tuple[list[Entry], list[Entry]]:
    """``(added, removed)``: candidate entries that the base does not have, and base entries the candidate lost."""
    remaining = Counter(_key(entry) for entry in base)
    added = []
    for entry in candidate:
        key = _key(entry)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            added.append(entry)
    # What is left in ``remaining`` are base entries that the candidate does not have.
    removed = []
    for entry in base:
        key = _key(entry)
        if remaining[key] > 0:
            remaining[key] -= 1
            removed.append(entry)
    return added, removed


def _exact_reversals(added: Sequence[Entry], base: Sequence[Entry]) -> set[int]:
    """The added entries that are exact reversals of a unique, unreversed base entry that only they claim."""
    from ..source_ids import source_id_variants

    by_spelling: dict[str, set[int]] = {}
    entries = {id(entry): entry for entry in base}
    reversed_in_base: set[str] = set()
    for entry in base:
        for spelling in source_id_variants(entry.source_id) if entry.source_id else ():
            by_spelling.setdefault(spelling, set()).add(id(entry))
        target = dict(entry.meta).get("reverses")
        if target:
            reversed_in_base |= source_id_variants(str(target))

    def original_of(entry: Entry) -> Entry | None:
        target = dict(entry.meta).get("reverses")
        if not target:
            return None
        spellings = source_id_variants(str(target))
        found = set().union(*(by_spelling.get(spelling, set()) for spelling in spellings))
        if len(found) != 1:
            return None  # no base original, or an ambiguous identity
        original = entries[next(iter(found))]
        return None if source_id_variants(original.source_id) & reversed_in_base else original

    originals = {id(entry): original_of(entry) for entry in added}
    claims = Counter(id(original) for original in originals.values() if original is not None)
    reversals: set[int] = set()
    for entry in added:
        original = originals[id(entry)]
        if original is None or claims[id(original)] != 1:
            continue
        if original.date == entry.date and Counter((p.account, p.amount, p.currency) for p in original.postings) \
                == Counter((p.account, -p.amount, p.currency) for p in entry.postings):
            reversals.add(id(entry))
    return reversals


def _opening_matches(request: CheckRequest, added: Sequence[Entry]) -> dict[int, int]:
    """``{authorization index: id of the one added entry it matches}``. An authorization that matches two or
    more entries, or none, is left out. Version 2 binds every ``Entry`` and ``Posting`` field (the entry
    fingerprint). Version 1 (weaker) binds every posting field only: account, amount, currency and metadata."""
    full = request.schema_version == FULL_ENTRY_REQUEST_SCHEMA_VERSION
    fingerprints = {id(entry): entry_fingerprint(entry) for entry in added} if full else {}
    matches: dict[int, int] = {}
    for index, auth in enumerate(request.openings):
        wanted = Counter(auth.postings)
        found = [entry for entry in added
                 if entry.date == auth.start_day and (entry.source_id or "") == auth.entry_source_id
                 and (fingerprints[id(entry)] == auth.entry_fingerprint if full else
                      Counter((p.account, p.amount, p.currency, tuple(sorted(p.meta)))
                              for p in entry.postings) == wanted)]
        if len(found) == 1:
            matches[index] = id(found[0])
    return matches


def _opening_match(request: CheckRequest, matches: Mapping[int, int], account: str, entry: Entry) -> int | None:
    found = request.policy.accounts.get(account)
    if found is None or found.state != PENDING_OPENING:
        return None
    for index, auth in enumerate(request.openings):
        if (auth.account == account and auth.start_day == found.start_day and auth.version == found.version
                and matches.get(index) == id(entry)):
            return index
    return None


def _prior_day_group(request: CheckRequest, base: Sequence[Entry], added: Sequence[Entry],
                     removed: Sequence[Entry], scope: set[str]) -> tuple[Entry | None, list[dict[str, Any]]]:
    """Check a version 3 group against the one added entry. ``(entry, [])`` when every rule holds.

    - The base has no entry, the candidate removes none and adds exactly one.
    - The entry has the authorized source ID and complete fingerprint, it is dated exactly ``start_day`` minus
      one day, and its metadata is exactly that of a fresh accrual opening: ``source-id``,
      ``import-source=accrual-opening``, ``accrual-opening=true``, ``coverage-id=opening`` and
      ``cutover-date=<start_day>``. A tracking opening or another opening kind is refused.
    - The protected accounts that the entry posts to (the same scope as the line check) are exactly the
      authorized accounts. A zero line on a protected account is refused, because the native preview leaves out
      zero lines.
    - For each account the current policy row is ``authorized_pending_opening`` with the authorized version and
      ``start_day``.

    Any failure gives ``BOOKS_OPENING_AUTHORIZATION_MISMATCH`` refusals and the group discharges nothing. This
    check does not prove the native plan, its support records or account open dates. The host replays the
    reviewed native plan for that.
    """
    first = request.openings[0]
    cutover = first.start_day
    problems: list[tuple[str | None, str]] = []
    entry = added[0] if len(added) == 1 else None
    if base or removed or entry is None:
        problems.append((None, "a fresh accrual opening needs empty books and exactly one added entry"))
    if entry is not None:
        meta = dict(entry.meta)
        if entry.source_id != first.entry_source_id or entry_fingerprint(entry) != first.entry_fingerprint:
            problems.append((None, "the added entry is not the reviewed entry"))
        if entry.date != cutover - timedelta(days=1):
            problems.append((None, f"the entry date must be {(cutover - timedelta(days=1)).isoformat()}, the day "
                                   f"before the cutover {cutover.isoformat()}"))
        if (len(meta) != len(entry.meta) or set(meta) != _PRIOR_DAY_META_KEYS
                or meta.get("source-id") != first.entry_source_id or meta.get("import-source") != "accrual-opening"
                or meta.get("accrual-opening") != "true" or meta.get("coverage-id") != "opening"
                or meta.get("cutover-date") != cutover.isoformat()):
            problems.append((None, "the entry is not a fresh accrual opening"))
        lines = [p for p in entry.postings if p.account in scope]
        if any(not p.amount for p in lines):
            problems.append((None, "the entry has a zero line on a protected account"))
        impacted = set(p.account for p in lines)
        authorized = {auth.account for auth in request.openings}
        for account in sorted(impacted - authorized):
            problems.append((account, "the entry posts to this protected account, and no authorization names it"))
        for account in sorted(authorized - impacted):
            problems.append((account, "an authorization names this account, and the entry does not post to it"))
    for auth in sorted(request.openings, key=lambda item: item.account):
        found = request.policy.accounts.get(auth.account)
        if (found is None or found.state != PENDING_OPENING or found.version != auth.version
                or found.start_day != auth.start_day):
            problems.append((auth.account, "the policy row is not the authorized pending opening"))
    refusals = []
    for account, detail in problems:
        found = request.policy.accounts.get(account) if account else None
        refusals.append(BooksPolicyError(
            OPENING_AUTHORIZATION_MISMATCH, f"The accrual opening authorization does not match: {detail}",
            "Preview the accrual opening again and review it. Nothing may be published.",
            account=account, state=found.state if found else None, start_day=cutover,
            entry_date=entry.date if entry is not None else None,
            source_id=(entry.source_id or "") if entry is not None else "", policy_epoch=request.policy.epoch,
            account_version=found.version if found else None).as_dict())
    return (None, refusals) if refusals else (entry, [])


def check_ledger_delta(request_document: Any, base_entries: Sequence[Entry], candidate_entries: Sequence[Entry],
                       mapped_accounts: Iterable[str] = ()) -> dict[str, Any]:
    """Check the entries that ``candidate_entries`` adds to ``base_entries``. A pure function: no environment.

    ``mapped_accounts`` carries scope evidence from the base and candidate workspaces. It includes bank and CSV
    mapping targets and accounts with an explicit bank, credit card or cash kind.
    Returns ``{"status": "ok" | "refused", "refusals": [...], ...}``; each refusal has the ``ledger.books_policy``
    refusal fields.
    """
    request = parse_request(request_document)
    added, removed = ledger_delta(base_entries, candidate_entries)
    refusals: list[dict[str, Any]] = [
        {"code": HISTORY_CHANGED, "message": "The proposed books do not keep an entry that is already in the books "
         f"({HISTORY_CHANGED}). Nothing may be published.", "action": "Publish only books that add entries.",
         "account": None, "state": None, "start_day": None, "entry_date": entry.date.isoformat(),
         "source_id": entry.source_id or "", "policy_epoch": request.policy.epoch, "account_version": None}
        for entry in removed]
    used: set[int] = set()
    if request.policy.enforced:
        scope = set(request.scope) | set(mapped_accounts) | set(request.policy.accounts)
        reversals = _exact_reversals(added, base_entries)
        prior_day = request.schema_version == PRIOR_DAY_REQUEST_SCHEMA_VERSION
        group_entry: Entry | None = None
        if prior_day:
            # The version 3 group is checked as a whole before the line check. Only a complete exact group
            # discharges the pending opening refusals of its one entry; versions 1 and 2 never match here.
            group_entry, mismatches = _prior_day_group(request, base_entries, added, removed, scope)
            refusals.extend(mismatches)
            matches: dict[int, int] = {}
        else:
            matches = _opening_matches(request, added)
        group = {auth.account: index for index, auth in enumerate(request.openings)} if group_entry else {}
        for entry in added:
            if id(entry) in reversals:
                continue
            for account in dict.fromkeys(p.account for p in entry.postings):
                if account not in scope:
                    continue
                refusal = request.policy.refusal(account, entry.date, entry.source_id or "")
                if refusal is None:
                    continue
                if refusal.code == OPENING_REQUIRED:
                    if prior_day:
                        match = group.get(account) if entry is group_entry else None
                    else:
                        match = _opening_match(request, matches, account, entry)
                    if match is not None:
                        used.add(match)
                        continue
                refusals.append(refusal.as_dict())
        if group_entry is not None and len(used) != len(request.openings):
            refusals.append(BooksPolicyError(
                OPENING_AUTHORIZATION_MISMATCH, "The accrual opening authorization does not match: an "
                "authorization was not used", "Preview the accrual opening again and review it. Nothing may "
                "be published.", entry_date=group_entry.date, source_id=group_entry.source_id or "",
                policy_epoch=request.policy.epoch).as_dict())
            used.clear()
    from .. import __version__
    return {"status": "refused" if refusals else "ok", "mode": request.policy.mode,
            "policy_epoch": request.policy.epoch, "checked_entries": len(added),
            "package_version": __version__, "request_sha256": request_digest(request_document),
            "openings_matched": [request.openings[index].operation_id for index in sorted(used)],
            "refusals": refusals}


def _workspace(entity_dir: Path) -> tuple[list[Entry], set[str]]:
    from ..entity import flat_bank_account_mappings
    from .store import LedgerStore

    config_path = entity_dir / "entity.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    scope = set(flat_bank_account_mappings(config).values())
    csv_mappings = config.get("csv_account_mappings") if isinstance(config, dict) else None
    if isinstance(csv_mappings, dict):
        scope.update(str(v["ledger_account"]) for v in csv_mappings.values()
                      if isinstance(v, dict) and v.get("ledger_account"))
    ledger = entity_dir / "ledger.sqlite"
    if not ledger.is_file():
        return [], scope
    store = LedgerStore(ledger)
    scope.update(account for account, kind in store.load_financial_kinds().items()
                  if kind in {"bank", "credit_card", "cash"})
    return store.load_entries(), scope


def request_digest(request_document: Any) -> str:
    """SHA-256 of the canonical JSON of a request: sorted keys, no spaces, UTF-8."""
    text = json.dumps(request_document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def workspace_digest(entity_dir: Path | str, *, ignore_empty_lock: bool = False) -> str:
    """SHA-256 of a workspace: the UTF-8 lines ``<sha256 of the file>  <relative POSIX path>\\n`` of every
    regular file, sorted by path (the format of ``sha256sum``). Empty folders do not count. A symbolic link or
    any other special file is refused. The optional normalization omits only the empty
    root engine lock file, which a caller may create when taking the workspace lock."""
    root = Path(entity_dir)
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"{root} is not a workspace folder")
    lines = []
    for folder, folders, files in os.walk(root, followlinks=False):
        for name in [*folders, *files]:
            path = Path(folder) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or "\n" in relative or "\r" in relative:
                raise ValueError(f"The workspace has a link or an unusual name: {relative!r}")
            if name in files:
                if not path.is_file():
                    raise ValueError(f"The workspace has a special file: {relative!r}")
                contents = path.read_bytes()
                if ignore_empty_lock and relative == ".books.lock" and not contents:
                    continue
                lines.append(f"{hashlib.sha256(contents).hexdigest()}  {relative}\n")
    return hashlib.sha256("".join(sorted(lines, key=lambda line: line[66:])).encode("utf-8")).hexdigest()


def check_workspaces(request_document: Any, base_dir: Path | str, candidate_dir: Path | str) -> dict[str, Any]:
    """``check_ledger_delta`` for two entity directories (``ledger.sqlite`` and ``entity.json`` of each).

    Both folders are copied to a private folder first. The copies are digested and checked, so the inputs are
    never changed and the digests are of the bytes that were checked.
    """
    with tempfile.TemporaryDirectory(prefix="books-policy-check-") as private:
        copies = []
        for label, source in (("base", Path(base_dir)), ("candidate", Path(candidate_dir))):
            workspace_digest(source)  # refuse links before the copy
            copy = Path(private) / label
            shutil.copytree(source, copy, symlinks=True)
            copies.append((copy, workspace_digest(copy)))
        (base_copy, base_sha), (candidate_copy, candidate_sha) = copies
        base_entries, base_scope = _workspace(base_copy)
        candidate_entries, candidate_scope = _workspace(candidate_copy)
        result = check_ledger_delta(request_document, base_entries, candidate_entries, base_scope | candidate_scope)
    return {**result, "base_sha256": base_sha, "candidate_sha256": candidate_sha}


RECEIPT_VERSION = 1
_COMMIT = re.compile(r"[0-9a-f]{40}")
_RESULT_FIELDS = ("status", "mode", "policy_epoch", "checked_entries", "openings_matched", "package_version",
                  "request_sha256", "base_sha256", "candidate_sha256")


def build_receipt(result: Mapping[str, Any], *, engine_build: str, company_id: str, job_id: str, job_nonce: str,
                  base_revision: int, request_bytes_sha256: str) -> dict[str, Any]:
    """Format a receipt from a ``check_workspaces`` result. This is not attestation.

    ``engine_build`` must be a full lowercase 40-character commit; a package version or a short commit is
    refused. The function only checks the form of its arguments. Any caller can pass any values, so the host
    must take ``engine_build`` from its own build or pin data, take the job fields from the job that it sent,
    and make and carry the receipt in a process and a channel that it trusts.
    """
    if not isinstance(engine_build, str) or not _COMMIT.fullmatch(engine_build):
        raise ValueError("engine_build must be the full 40-character lowercase commit of the pinned engine")
    missing = [name for name in _RESULT_FIELDS if name not in result]
    if missing:
        raise ValueError(f"the check result has no {missing[0]}")
    if not all(isinstance(v, str) and v for v in (company_id, job_id, job_nonce, request_bytes_sha256)) \
            or isinstance(base_revision, bool) or not isinstance(base_revision, int):
        raise ValueError("the job binding fields must be non-empty text and an integer base_revision")
    return {"receipt_version": RECEIPT_VERSION, "engine_build": engine_build, "company_id": company_id,
            "job_id": job_id, "job_nonce": job_nonce, "base_revision": base_revision,
            "request_bytes_sha256": request_bytes_sha256, **{name: result[name] for name in _RESULT_FIELDS},
            "refusal_count": len(result.get("refusals", []))}


def receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    """The canonical bytes of a receipt (sorted keys, no spaces, UTF-8), for a host to hash or sign."""
    return json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -I -m bookkeeping.ledger.books_policy_check")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Check a proposed workspace against a host books start policy.")
    check.add_argument("--request", type=Path, required=True)
    check.add_argument("--base", type=Path, required=True)
    check.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        document = json.loads(args.request.read_text(encoding="utf-8"))
        result = check_workspaces(document, args.base, args.candidate)
    except BooksPolicyError as exc:
        print(json.dumps({"status": "error", "error": exc.as_dict()}))
        return 2
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": {"code": "BOOKS_CHECK_INPUT_INVALID",
                                                       "message": f"{type(exc).__name__}: {exc}"}}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
