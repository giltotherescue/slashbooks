"""QuickBooks category-agreement backtest on held-out history.

``books qb agreement run`` answers one question for a company moving from
QuickBooks: on transactions Slashbooks never learned from, how often does its
suggested category match the category QuickBooks recorded?

QuickBooks history stays reference evidence. This module reads the saved
sync snapshot (``sources/quickbooks/<digest>/``). It never posts to the ledger
and never writes ``learned-context``. The only file it writes is the result
document under ``reports/quickbooks-agreement/``.

Method:
  1. Take the single-account Purchase and Bill transactions of the snapshot.
  2. Split them: a training slice and a held-out slice (see ``split_history``).
  3. Build counterparty precedents from the training slice only, in memory.
  4. For each held-out transaction, suggest a category in the same order a
     real suggestion uses: taught category, learned-context category, then the
     precedent. The held-out slice never feeds a precedent.
  5. Compare with the QuickBooks category, mapped through the snapshot account
     mapping and any approved account crosswalk.

A transaction with no suggestion is counted apart. It is never a disagreement.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_HOLDOUT = "3"
REPORT_DIR = Path("reports") / "quickbooks-agreement"
_SCORED_TYPES = {"Purchase": "EntityRef", "Bill": "VendorRef"}
_SAMPLE_LIMIT = 5
_CENT = Decimal("0.01")
_RATE = Decimal("0.0001")


class AgreementError(ValueError):
    """A refusal with a plain reason."""


@dataclass(frozen=True)
class Txn:
    ref: str  # "<Type>:<Id>", unique across types
    date: date
    counterparty: str  # display name, as QuickBooks wrote it
    key: str  # normalized counterparty key; "" when absent
    amount: Decimal  # absolute value
    category: str  # QuickBooks category after account mapping


def _dump(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _rate(numerator: Decimal | int, denominator: Decimal | int) -> float | None:
    if not denominator:
        return None
    return float((Decimal(numerator) / Decimal(denominator)).quantize(_RATE, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Holdout specification
# ---------------------------------------------------------------------------

def parse_holdout(text: str) -> tuple[str, Decimal | int]:
    """Return ("months", N) or ("fraction", F) for a --holdout value.

    ``3`` and ``3m`` mean three months. ``0.2`` and ``20%`` mean a fraction.
    """
    raw = str(text).strip().lower()
    match = re.fullmatch(r"(\d+)\s*(?:m|mo|month|months)?", raw)
    if match:
        months = int(match.group(1))
        if not 1 <= months <= 120:
            raise AgreementError("Holdout months must be between 1 and 120.")
        return "months", months
    match = re.fullmatch(r"(\d*\.\d+|\d+\.)", raw) or re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", raw)
    if match:
        fraction = Decimal(match.group(1))
        if raw.endswith("%"):
            fraction = fraction / 100
        if not Decimal(0) < fraction < Decimal(1):
            raise AgreementError("Holdout fraction must be greater than 0 and less than 1.")
        return "fraction", fraction.normalize()
    raise AgreementError("Holdout must be a number of months (3) or a fraction (0.2 or 20%).")


def _month_start(as_of: date, months: int) -> date:
    index = as_of.year * 12 + (as_of.month - 1) - (months - 1)
    return date(index // 12, index % 12 + 1, 1)


def split_history(txns: list[Txn], method: str, value: Decimal | int, as_of: date,
                  snapshot_id: str) -> tuple[list[Txn], list[Txn], date | None]:
    """Split transactions dated on or before ``as_of`` into (training, held_out, start).

    By months the held-out slice is the as-of month and the months before it,
    and training is everything earlier. By fraction the held-out slice is a
    stable pseudo-random sample chosen by a hash of the transaction reference,
    so the same snapshot and fraction always choose the same transactions.
    """
    window = [t for t in txns if t.date <= as_of]
    if method == "months":
        start = _month_start(as_of, int(value))
        return ([t for t in window if t.date < start], [t for t in window if t.date >= start], start)
    ordered = sorted(window, key=lambda t: hashlib.sha256(
        f"{snapshot_id}:{value}:{t.ref}".encode()).hexdigest())
    count = int((Decimal(len(window)) * Decimal(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    held = {t.ref for t in ordered[:count]}
    return ([t for t in window if t.ref not in held], [t for t in window if t.ref in held], None)


# ---------------------------------------------------------------------------
# Reading QuickBooks reference history
# ---------------------------------------------------------------------------

def _load_snapshot(entity, snapshot_id: str | None) -> tuple[str, dict, list[dict]]:
    root = entity.path / "sources" / "quickbooks"
    if not root.is_dir():
        raise AgreementError("No QuickBooks history found. Import a QuickBooks snapshot first.")
    candidates = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
        if snapshot_id and folder.name != snapshot_id:
            continue
        source, manifest = folder / "snapshot.json", folder / "manifest.json"
        if source.is_file() and manifest.is_file() and not source.is_symlink():
            candidates.append(folder)
    if not candidates:
        raise AgreementError("No QuickBooks history found. Import a QuickBooks snapshot first."
                             if not snapshot_id else f"QuickBooks snapshot {snapshot_id} was not found.")
    loaded = []
    for folder in candidates:
        raw = (folder / "snapshot.json").read_bytes()
        try:
            snapshot = json.loads(raw)
            accounts = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["accounts"]
        except (ValueError, KeyError) as exc:
            raise AgreementError(f"QuickBooks snapshot {folder.name} cannot be read.") from exc
        loaded.append((str(snapshot.get("fetched_at", "")), folder.name, snapshot, accounts))
    loaded.sort(key=lambda row: (row[0], row[1]))
    _, name, snapshot, accounts = loaded[-1]  # newest fetch; ties by digest
    return name, snapshot, accounts


def _crosswalk(entity) -> dict[str, str]:
    from .compare import _qbo_account_crosswalk
    return _qbo_account_crosswalk(entity)


def _transactions(snapshot: dict, accounts: list[dict], crosswalk: dict[str, str]):
    """Return (scorable transactions, exclusion counts)."""
    from .ledger.normalize import normalize_description
    by_id = {str(a["id"]): a for a in accounts}
    excluded: Counter = Counter()
    txns: list[Txn] = []
    for kind, items in sorted(snapshot.get("entities", {}).items()):
        if kind not in _SCORED_TYPES:
            excluded[f"type:{kind}"] += len(items)
            continue
        for item in items:
            lines = item.get("Line", [])
            based = [l for l in lines if l.get("DetailType") == "AccountBasedExpenseLineDetail"]
            if not lines or len(based) != len(lines):
                excluded["item_based_or_empty"] += 1
                continue
            refs = {str(l["AccountBasedExpenseLineDetail"].get("AccountRef", {}).get("value", "")) for l in based}
            if len(refs) != 1:
                excluded["split"] += 1
                continue
            account = by_id.get(next(iter(refs)))
            if account is None or not item.get("TxnDate"):
                excluded["unresolved_account_or_date"] += 1
                continue
            category = crosswalk.get(str(account["name"]), str(account["mapped_account"]))
            name = item.get(_SCORED_TYPES[kind], {}).get("name", "")
            name = name.strip() if isinstance(name, str) else ""
            txns.append(Txn(
                ref=f"{kind}:{item['Id']}", date=date.fromisoformat(item["TxnDate"]),
                counterparty=name, key=normalize_description(name) if name else "",
                amount=abs(sum((Decimal(str(l["Amount"])) for l in based), Decimal(0))),
                category=category))
    txns.sort(key=lambda t: (t.date, t.ref))
    return txns, dict(sorted(excluded.items()))


# ---------------------------------------------------------------------------
# Suggestion path (read-only)
# ---------------------------------------------------------------------------

def build_precedents(training: list[Txn]) -> dict[str, str]:
    """Plurality category per counterparty key, from the training slice only.

    A tie between categories gives no precedent, as the categorizer would have
    no single answer to suggest.
    """
    counts: dict[str, Counter] = defaultdict(Counter)
    for txn in training:
        if txn.key:
            counts[txn.key][txn.category] += 1
    precedents = {}
    for key, counter in counts.items():
        ranked = counter.most_common(2)
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            precedents[key] = ranked[0][0]
    return precedents


def suggest(entity, key: str, precedents: dict[str, str]) -> tuple[str, str] | None:
    """Return (category, source) or None. Order: taught, learned, precedent."""
    if not key:
        return None
    from .queue import load_learned_context, taught_for
    taught = taught_for(entity, key)
    if taught:
        return str(taught["category"]), "taught"
    entry = load_learned_context(entity).get(key)
    if isinstance(entry, dict) and entry.get("canonical_category") and not entry.get("reset", False):
        return str(entry["canonical_category"]), "learned"
    if key in precedents:
        return precedents[key], "precedent"
    return None


def _local_catalog(entity) -> set[str] | None:
    try:
        from .ledger.store import LedgerStore, default_store_path
        names = LedgerStore(default_store_path(entity.path)).load_account_names()
    except Exception:
        return None
    return names or None


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def _label(method: str, value: Decimal | int) -> str:
    return f"{value}m" if method == "months" else f"f{str(value).replace('.', '_')}"


def run_agreement(entity, *, holdout: str = DEFAULT_HOLDOUT, as_of: date | None = None,
                  snapshot_id: str | None = None) -> dict:
    method, value = parse_holdout(holdout)
    sid, snapshot, accounts = _load_snapshot(entity, snapshot_id)
    crosswalk = _crosswalk(entity)
    txns, excluded = _transactions(snapshot, accounts, crosswalk)
    if not txns:
        raise AgreementError("The QuickBooks history has no purchases or bills that can be scored.")
    as_of = as_of or max(t.date for t in txns)
    training, held, start = split_history(txns, method, value, as_of, sid)
    if not held:
        raise AgreementError("The held-out slice is empty. Choose a longer holdout or a later --as-of date.")
    held_refs = {t.ref for t in held}
    if held_refs & {t.ref for t in training}:  # structural guard; split_history keeps them disjoint
        raise AgreementError("Training and held-out slices overlap.")
    precedents = build_precedents(training)

    catalog = _local_catalog(entity)
    qb_categories = {t.category for t in txns}
    agree: list[tuple[Txn, str]] = []
    disagree: list[tuple[Txn, str, str, str]] = []
    nothing: list[Txn] = []
    sources: Counter = Counter()
    for txn in held:
        found = suggest(entity, txn.key, precedents)
        if found is None:
            nothing.append(txn)
            continue
        category, source = found
        sources[source] += 1
        if category == txn.category:
            agree.append((txn, source))
            continue
        if (catalog is not None and txn.category not in catalog) or category not in qb_categories:
            kind = "judgment-mapping"
        else:
            kind = "category_disagreement"
        disagree.append((txn, category, source, kind))

    def total(items) -> Decimal:
        return sum((t.amount for t in items), Decimal(0))

    held_total = total(held)
    suggested = len(agree) + len(disagree)
    agree_amount = total(t for t, _ in agree)
    disagree_amount = total(d[0] for d in disagree)
    totals = {
        "scored": len(held), "scored_amount": _money(held_total),
        "suggested": suggested, "agree": len(agree), "disagree": len(disagree),
        "no_suggestion": len(nothing),
        "agree_amount": _money(agree_amount), "disagree_amount": _money(disagree_amount),
        "no_suggestion_amount": _money(total(nothing)),
        "agreement_rate": _rate(len(agree), suggested),
        "agreement_rate_amount": _rate(agree_amount, agree_amount + disagree_amount),
        "suggestion_rate": _rate(suggested, len(held)),
        "agreement_rate_of_all": _rate(len(agree), len(held)),
    }

    per_account: dict[str, dict] = {}
    def bucket(cat: str) -> dict:
        return per_account.setdefault(cat, {"quickbooks_category": cat, "scored": 0, "agree": 0,
                                            "disagree": 0, "no_suggestion": 0,
                                            "_agree_amt": Decimal(0), "_dis_amt": Decimal(0)})
    for txn in held:
        bucket(txn.category)["scored"] += 1
    for txn, _ in agree:
        row = bucket(txn.category); row["agree"] += 1; row["_agree_amt"] += txn.amount
    for txn, *_ in disagree:
        row = bucket(txn.category); row["disagree"] += 1; row["_dis_amt"] += txn.amount
    for txn in nothing:
        bucket(txn.category)["no_suggestion"] += 1
    by_account = []
    for cat in sorted(per_account):
        row = per_account[cat]
        suggested_n = row["agree"] + row["disagree"]
        by_account.append({
            "quickbooks_category": cat, "scored": row["scored"], "agree": row["agree"],
            "disagree": row["disagree"], "no_suggestion": row["no_suggestion"],
            "agreement_rate": _rate(row["agree"], suggested_n),
            "agreement_rate_amount": _rate(row["_agree_amt"], row["_agree_amt"] + row["_dis_amt"])})

    groups: dict[tuple, dict] = {}
    for txn, category, source, kind in sorted(disagree, key=lambda d: d[0].ref):
        group = groups.setdefault((txn.key, txn.category, category), {
            "counterparty": txn.counterparty, "quickbooks_category": txn.category,
            "slashbooks_category": category, "classification": kind, "suggestion_source": source,
            "count": 0, "_total": Decimal(0), "sample_ids": []})
        group["count"] += 1
        group["_total"] += txn.amount
        if len(group["sample_ids"]) < _SAMPLE_LIMIT:
            group["sample_ids"].append(txn.ref)
    disagreements = []
    for group in sorted(groups.values(), key=lambda g: (-g["_total"], g["counterparty"],
                                                         g["quickbooks_category"], g["slashbooks_category"])):
        group = dict(group, total=_money(group.pop("_total")))
        disagreements.append(group)

    missing: dict[str, dict] = {}
    for txn in nothing:
        row = missing.setdefault(txn.key, {"counterparty": txn.counterparty, "count": 0, "_total": Decimal(0)})
        row["count"] += 1
        row["_total"] += txn.amount
    no_suggestion_by_counterparty = [
        dict(row, total=_money(row.pop("_total"))) for row in sorted(
            (dict(r) for r in missing.values()), key=lambda r: (-r["_total"], r["counterparty"]))]

    digest_inputs = {"snapshot": sid, "method": method, "value": str(value), "as_of": as_of.isoformat(),
                     "crosswalk": crosswalk, "schema": SCHEMA_VERSION,
                     "training": sorted(t.ref for t in training), "held_out": sorted(held_refs),
                     "precedents": sorted(precedents.items()),
                     "sources": sorted(sources.items())}
    relative = REPORT_DIR / sid / f"holdout-{_label(method, value)}-asof-{as_of.isoformat()}.json"
    warnings = []
    if not training:
        warnings.append("The training slice is empty. Only taught or learned categories can give a suggestion.")
    result = {
        "success": True, "kind": "quickbooks_agreement", "schema_version": SCHEMA_VERSION,
        "snapshot_id": sid,
        "holdout": {"method": method, "value": float(value) if method == "fraction" else int(value),
                    "as_of": as_of.isoformat(), "start": start.isoformat() if start else None},
        "slices": {"training": len(training), "held_out": len(held),
                   "training_range": [training[0].date.isoformat(), training[-1].date.isoformat()] if training else None,
                   "held_out_range": [min(t.date for t in held).isoformat(), max(t.date for t in held).isoformat()]},
        "excluded_from_scoring": excluded,
        "totals": totals, "by_account": by_account, "disagreements": disagreements,
        "no_suggestion": {"count": len(nothing), "total": _money(total(nothing)),
                          "by_counterparty": no_suggestion_by_counterparty},
        "suggestion_sources": dict(sorted(sources.items())),
        "inputs_digest": hashlib.sha256(_dump(digest_inputs)).hexdigest(),
        "result_path": relative.as_posix(),
        "warnings": warnings,
    }
    _write_result(entity.path, relative, result)
    return result


def _write_result(entity_path: Path, relative: Path, result: dict) -> None:
    target = entity_path / relative
    reports = (entity_path / "reports").resolve()
    if not target.parent.resolve(strict=False).is_relative_to(reports) or any(
            p.is_symlink() for p in (entity_path / "reports", target.parent)):
        raise AgreementError("Result directory escapes the company reports folder.")
    payload = _dump(result)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and not target.is_symlink() and target.read_bytes() == payload:
        return
    fd, tmp = tempfile.mkstemp(prefix=".agreement-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_parser(qb_sub) -> None:
    agreement = qb_sub.add_parser(
        "agreement", help="Backtest category agreement with QuickBooks on held-out history")
    commands = agreement.add_subparsers(dest="agreement_command", required=True)
    run = commands.add_parser(
        "run", help="Score Slashbooks category suggestions against QuickBooks on a held-out slice")
    run.add_argument("--entity", type=Path, required=True, dest="entity_path")
    run.add_argument("--holdout", default=DEFAULT_HOLDOUT,
                     help="Held-out slice: months (3, the default) or a fraction (0.2 or 20%%)")
    run.add_argument("--as-of", type=date.fromisoformat, dest="as_of",
                     help="Last date to score (YYYY-MM-DD); default is the latest QuickBooks transaction")
    run.add_argument("--snapshot-id", dest="snapshot_id",
                     help="QuickBooks snapshot digest; default is the newest saved snapshot")
    run.add_argument("--json", action="store_true", dest="as_json", help="Print the result as JSON")


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def run(args) -> int:
    import sys
    from .entity import load_entity
    try:
        entity = load_entity(args.entity_path)
        result = run_agreement(entity, holdout=args.holdout, as_of=args.as_of, snapshot_id=args.snapshot_id)
    except (AgreementError, ValueError, KeyError, TypeError, OSError) as exc:
        if args.as_json:
            print(json.dumps({"success": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"Agreement backtest refused: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    t = result["totals"]
    print(f"Slashbooks agrees with QuickBooks on {_pct(t['agreement_rate'])} of {t['suggested']} "
          f"held-out transactions with a suggestion ({_pct(t['agreement_rate_amount'])} by amount).")
    print(f"Held out {t['scored']}, no suggestion {t['no_suggestion']}, "
          f"disagree {t['disagree']}, trained on {result['slices']['training']}.")
    for row in result["disagreements"][:10]:
        print(f"  {row['counterparty']}: QuickBooks {row['quickbooks_category']} / "
              f"Slashbooks {row['slashbooks_category']} ({row['count']}, {row['total']})")
    print(f"Result saved to {result['result_path']}")
    return 0
