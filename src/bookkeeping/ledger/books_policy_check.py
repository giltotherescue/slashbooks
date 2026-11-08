"""Trusted check of a proposed ledger change against a host's books start policy.

The check inside a command (``SLASHBOOKS_BOOKS_POLICY``, ``ledger.books_policy``) gives early feedback, but
it is not the fence. A session that runs the engine can remove the variable or the file, drop accounts from
the document, or set ``mode`` to ``legacy``. The host's policy epoch does not change in any of these cases,
so an epoch check at publication does not find them.

This module is the fence. The host runs it in a process that the agent cannot configure, after the engine
command and before publication:

    python -I -m bookkeeping.ledger.books_policy_check check --request REQUEST.json \\
        --base BASE_ENTITY_DIR --candidate CANDIDATE_ENTITY_DIR

* ``REQUEST.json`` comes from the host's immutable job snapshot, never from the workspace::

      {"schema_version": 1,
       "policy": {<the books start policy document, ledger.books_policy>},
       "scope": ["Assets:Bank:Checking", "Liabilities:Card-1234"],
       "opening_authorizations": [
         {"account": "Assets:Bank:Checking", "start_day": "2026-03-15", "version": 1,
          "operation": "quickbooks_sync_opening", "operation_id": "<host receipt ID>",
          "entry_source_id": "<source ID of the committed opening entry>",
          "postings": [["Assets:Bank:Checking", "100.00", "USD"], ["Equity:Opening-Balances", "-100.00", "USD"]]}]}

* ``BASE_ENTITY_DIR`` is the workspace that the host published before the job. ``CANDIDATE_ENTITY_DIR`` is
  the workspace that the job wants to publish.

The check never reads ``SLASHBOOKS_BOOKS_POLICY`` or any other environment variable. It compares the two
ledgers. Every base entry must still be in the candidate (``BOOKS_LEDGER_HISTORY_CHANGED``). Every entry that
the candidate adds is then checked line by line with the policy rules of ``ledger.books_policy``. The scope
is the request ``scope``, every account that the policy names, and every ledger account that the base or the
candidate maps from a bank or CSV source. Many provider accounts can map to one ledger account; the policy is
for the ledger account.

The ledger is compared as a multiset of entries, so the order of entries does not matter. The order of the
postings inside an entry is part of the entry, so a change of that order is an edit.

Only two things let an added line through a refusal:

* The added entry is the exact reversal of a base entry: the same date, the same postings with each amount
  negated. The original must be the only base entry with that source ID (in any spelling), no base entry may
  reverse it already, and no other added entry may claim it. If two added entries claim one original, neither
  is exempt. A reversal of an added entry is never exempt, so two new entries cannot exempt each other.
* The account is ``authorized_pending_opening`` and an opening authorization from the host matches the entry
  exactly: the account, ``start_day`` and ``version`` of the policy entry, the entry date, the entry source ID
  and every posting of the entry (account, amount, currency and posting metadata, exactly). Two
  authorizations for one account, start day and version make the request invalid. An authorization that
  matches more than one added entry matches none. The host derives an authorization only from committed
  evidence of a reviewed opening operation (its own receipt). Entry metadata such as ``import-source``, a
  source ID alone or ``reverses`` never counts.

``check_workspaces`` copies both workspaces to a private folder first, and checks and digests the copies. It
never changes its inputs. Its result has the fields that a host receipt binds: ``engine_version``,
``request_sha256`` (``request_digest``), ``base_sha256`` and ``candidate_sha256`` (``workspace_digest``). A
receipt is only as trusted as the process that makes it and the channel that carries it; these digests let the
host bind the receipt to the exact bytes that it publishes.

Exit code 0 means ``ok``, 1 means ``refused`` (nothing may be published), 2 means a bad request or workspace.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .books_policy import OPENING_REQUIRED, PENDING_OPENING, BooksPolicy, BooksPolicyError, _invalid, parse_policy
from .model import Entry

REQUEST_SCHEMA_VERSION = 1
HISTORY_CHANGED = "BOOKS_LEDGER_HISTORY_CHANGED"
OPENING_OPERATIONS = ("quickbooks_sync_opening", "quickbooks_import_opening", "statement_opening",
                      "starting_balance", "accrual_opening")


@dataclass(frozen=True)
class OpeningAuthorization:
    account: str
    start_day: date
    version: int
    operation: str
    operation_id: str
    entry_source_id: str
    postings: tuple[tuple[str, Decimal, str, tuple[tuple[str, str], ...]], ...]


@dataclass(frozen=True)
class CheckRequest:
    policy: BooksPolicy
    scope: frozenset[str]
    openings: tuple[OpeningAuthorization, ...]


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{where} must be non-empty text")
    return value


def _authorization(raw: Any, index: int) -> OpeningAuthorization:
    where = f"opening_authorizations[{index}]"
    fields = {"account", "start_day", "version", "operation", "operation_id", "entry_source_id", "postings"}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise _invalid(f"{where} must have exactly the fields {', '.join(sorted(fields))}")
    try:
        start = date.fromisoformat(_text(raw["start_day"], f"{where}.start_day"))
    except ValueError:
        raise _invalid(f"{where}.start_day must be a YYYY-MM-DD date") from None
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise _invalid(f"{where}.version must be an integer of 1 or more")
    if raw["operation"] not in OPENING_OPERATIONS:
        raise _invalid(f"{where}.operation must be one of {', '.join(OPENING_OPERATIONS)}")
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
    if document.get("schema_version") != REQUEST_SCHEMA_VERSION or isinstance(document.get("schema_version"), bool):
        raise _invalid(f"the check request schema_version must be {REQUEST_SCHEMA_VERSION}")
    if "policy" not in document or "scope" not in document:
        raise _invalid("the check request needs policy and scope")
    scope = document["scope"]
    if not isinstance(scope, list):
        raise _invalid("scope must be a list of ledger accounts")
    raw_openings = document.get("opening_authorizations", [])
    if not isinstance(raw_openings, list):
        raise _invalid("opening_authorizations must be a list")
    openings = tuple(_authorization(item, index) for index, item in enumerate(raw_openings))
    keys = Counter((auth.account, auth.start_day, auth.version) for auth in openings)
    twice = sorted(key for key, count in keys.items() if count > 1)
    if twice:
        raise _invalid(f"more than one authorization names {twice[0][0]} on {twice[0][1].isoformat()} "
                       f"version {twice[0][2]}")
    return CheckRequest(parse_policy(document["policy"]), frozenset(_text(a, "scope item") for a in scope), openings)


def _key(entry: Entry) -> tuple:
    return (entry.date.isoformat(), entry.flag, entry.narration, entry.payee, entry.tags, entry.links, entry.meta,
            tuple((p.account, str(p.amount), p.currency) for p in entry.postings))


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
    more entries, or none, is left out. Every posting field is bound: account, amount, currency and metadata."""
    matches: dict[int, int] = {}
    for index, auth in enumerate(request.openings):
        wanted = Counter(auth.postings)
        found = [entry for entry in added
                 if entry.date == auth.start_day and (entry.source_id or "") == auth.entry_source_id
                 and Counter((p.account, p.amount, p.currency, tuple(sorted(p.meta)))
                             for p in entry.postings) == wanted]
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


def check_ledger_delta(request_document: Any, base_entries: Sequence[Entry], candidate_entries: Sequence[Entry],
                       mapped_accounts: Iterable[str] = ()) -> dict[str, Any]:
    """Check the entries that ``candidate_entries`` adds to ``base_entries``. A pure function: no environment.

    ``mapped_accounts`` are the ledger accounts that the base and the candidate map from bank or CSV sources.
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
        matches = _opening_matches(request, added)
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
                    match = _opening_match(request, matches, account, entry)
                    if match is not None:
                        used.add(match)
                        continue
                refusals.append(refusal.as_dict())
    from .. import __version__
    return {"status": "refused" if refusals else "ok", "mode": request.policy.mode,
            "policy_epoch": request.policy.epoch, "checked_entries": len(added),
            "engine_version": __version__, "request_sha256": request_digest(request_document),
            "openings_matched": [request.openings[index].operation_id for index in sorted(used)],
            "refusals": refusals}


def _workspace(entity_dir: Path) -> tuple[list[Entry], set[str]]:
    from ..entity import flat_bank_account_mappings
    from .store import LedgerStore

    config_path = entity_dir / "entity.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    mapped = set(flat_bank_account_mappings(config).values())
    csv_mappings = config.get("csv_account_mappings") if isinstance(config, dict) else None
    if isinstance(csv_mappings, dict):
        mapped.update(str(v["ledger_account"]) for v in csv_mappings.values()
                      if isinstance(v, dict) and v.get("ledger_account"))
    ledger = entity_dir / "ledger.sqlite"
    return (LedgerStore(ledger).load_entries() if ledger.is_file() else []), mapped


def request_digest(request_document: Any) -> str:
    """SHA-256 of the canonical JSON of a request: sorted keys, no spaces, UTF-8."""
    text = json.dumps(request_document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def workspace_digest(entity_dir: Path | str) -> str:
    """SHA-256 of a workspace: the UTF-8 lines ``<sha256 of the file>  <relative POSIX path>\\n`` of every
    regular file, sorted by path (the format of ``sha256sum``). Empty folders do not count. A symbolic link or
    any other special file is refused."""
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
                lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n")
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
        base_entries, base_mapped = _workspace(base_copy)
        candidate_entries, candidate_mapped = _workspace(candidate_copy)
        result = check_ledger_delta(request_document, base_entries, candidate_entries, base_mapped | candidate_mapped)
    return {**result, "base_sha256": base_sha, "candidate_sha256": candidate_sha}


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
