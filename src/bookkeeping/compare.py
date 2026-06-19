"""QuickBooks comparison, confidence package, and backtest orchestration.

Public API
----------
compare_period(entity, qb_folder, from_date, to_date) -> ComparisonReport
    Multi-grain diff: P&L categories, BS accounts, TB totals, GL transaction
    counts, and unmatched transactions both directions.

confidence_package(entity, qb_folder, from_date, to_date) -> dict
    Full migration-confidence artefact including the readiness recap, source
    coverage matrix, match summary, categorized differences, missing-data items,
    spot-audit sample, and plain-English Markdown summary.
    Writes to <entity>/reports/migration-confidence/.

load_differences(entity) -> list[dict]
    Read the persisted differences.json (returns [] when absent).

save_differences(entity, diffs) -> None
    Atomic write of differences.json, preserving accepted/fixed dispositions.

CLI surface (wired in by orchestrator — do NOT edit cli.py)
-----------------------------------------------------------
add_parser(subparsers)
run(args)

Subcommands:
  backtest run  --entity PATH --qb-folder PATH --from DATE --to DATE
                [--skip-fetch] [--banksync-json FILE ...]  [--partial]
  compare period --entity PATH --qb-folder PATH --from DATE --to DATE
  compare categorize-diff --key K --category C --note N --entity PATH
  compare accept-diff --key K --note N --entity PATH

Difference-key scheme
---------------------
Keys are the tuple (grain, account_or_category) serialised as
"<grain>:<account_or_category>", e.g.:
    "pnl:Income:Revenue:Consulting"
    "bs:Assets:Bank:Checking"
    "tb:total"
    "gl-count:Expenses:Software"
    "unmatched-ours:<source_id>"
    "unmatched-qb:<date>|<amount>|<norm_desc>"

Materiality (OQ3 defaults, pending owner sign-off)
---------------------------------------------------
A difference is material when:
    abs(delta) >= 25.00  OR  abs(delta) >= 0.01 * max(abs(ours), abs(reference))
Both thresholds are configurable via entity.json under a "compare" section:
    { "compare": { "material_abs": 25.00, "material_pct": 0.01 } }

Categorization heuristics
--------------------------
Deterministic heuristics applied where possible; default is "uncategorized":
- timing-pending : delta equals (within 0.01) the sum of staged pending amounts
  near the period end (within 3 days).
- judgment-mapping: account exists only on one side (ours or reference).
- uncategorized : all other cases (owner/agent assigns via categorize-diff).
- Categories "our-bug", "missing-source-data", "likely-reference-error" are not
  auto-assigned — they require owner judgement.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from .ledger.normalize import normalize_description
from .quickbooks import (
    ReadinessReport,
    inventory,
    iter_export_files,
    parse_balance_sheet,
    parse_profit_and_loss,
    parse_trial_balance,
    parse_general_ledger,
    map_qb_account,
    _reset_collision_registry,
)
from .reports.statements import (
    profit_and_loss as stmt_pnl,
    balance_sheet as stmt_bs,
    trial_balance as stmt_tb,
    general_ledger as stmt_gl,
)
from .entity import Entity, load_entity

# ---------------------------------------------------------------------------
# Bank/card account type keys (used to filter GL sections for matching)
# ---------------------------------------------------------------------------

_BANK_CARD_QB_TYPES = frozenset(["bank", "credit card"])

# ---------------------------------------------------------------------------
# Materiality helpers
# ---------------------------------------------------------------------------

_DEFAULT_MATERIAL_ABS = Decimal("25.00")
_DEFAULT_MATERIAL_PCT = Decimal("0.01")


def _materiality_thresholds(entity: Entity) -> tuple[Decimal, Decimal]:
    """Return (abs_threshold, pct_threshold) from entity config or defaults."""
    compare_cfg = entity.entity_config.get("compare", {})
    abs_t = Decimal(str(compare_cfg.get("material_abs", _DEFAULT_MATERIAL_ABS)))
    pct_t = Decimal(str(compare_cfg.get("material_pct", _DEFAULT_MATERIAL_PCT)))
    return abs_t, pct_t


def _is_material(delta: Decimal, ours: Decimal, reference: Decimal,
                 abs_t: Decimal, pct_t: Decimal) -> bool:
    """Return True when the difference is material."""
    abs_delta = abs(delta)
    if abs_delta >= abs_t:
        return True
    larger = max(abs(ours), abs(reference))
    if larger > Decimal("0") and abs_delta >= pct_t * larger:
        return True
    return False


# ---------------------------------------------------------------------------
# Difference record
# ---------------------------------------------------------------------------

_VALID_CATEGORIES = frozenset([
    "our-bug",
    "missing-source-data",
    "timing-pending",
    "judgment-mapping",
    "likely-reference-error",
    "uncategorized",
])

_VALID_STATUSES = frozenset(["open", "accepted", "fixed"])


@dataclass
class DiffRecord:
    """A single material difference between our books and the QB reference."""
    key: str                  # "<grain>:<account_or_id>"
    grain: str                # "pnl" | "bs" | "tb" | "gl-count" | "unmatched-ours" | "unmatched-qb"
    account: str              # human-readable account/category label
    ours: Decimal
    reference: Decimal
    delta: Decimal            # ours - reference
    category: str             # one of _VALID_CATEGORIES
    status: str               # "open" | "accepted" | "fixed"
    note: str = ""
    re_opened: bool = False   # True when a previously accepted/fixed diff materially changed


def _diff_key(grain: str, account: str) -> str:
    return f"{grain}:{account}"


def _diff_record(
    grain: str,
    account: str,
    ours: Decimal,
    reference: Decimal,
    category: str = "uncategorized",
    status: str = "open",
    note: str = "",
) -> DiffRecord:
    delta = ours - reference
    return DiffRecord(
        key=_diff_key(grain, account),
        grain=grain,
        account=account,
        ours=ours,
        reference=reference,
        delta=delta,
        category=category,
        status=status,
        note=note,
    )


def _diff_to_dict(d: DiffRecord) -> dict:
    return {
        "key": d.key,
        "grain": d.grain,
        "account": d.account,
        "ours": str(d.ours),
        "reference": str(d.reference),
        "delta": str(d.delta),
        "category": d.category,
        "status": d.status,
        "note": d.note,
        "re_opened": d.re_opened,
    }


def _dict_to_diff(d: dict) -> DiffRecord:
    return DiffRecord(
        key=d["key"],
        grain=d["grain"],
        account=d["account"],
        ours=Decimal(str(d["ours"])),
        reference=Decimal(str(d["reference"])),
        delta=Decimal(str(d["delta"])),
        category=d.get("category", "uncategorized"),
        status=d.get("status", "open"),
        note=d.get("note", ""),
        re_opened=bool(d.get("re_opened", False)),
    )


# ---------------------------------------------------------------------------
# Unmatched transaction records
# ---------------------------------------------------------------------------

@dataclass
class UnmatchedTransaction:
    """A transaction present on one side but not found on the other."""
    side: str             # "ours" | "qb"
    txn_date: date
    amount: Decimal
    description: str
    account: str
    source_id: str = ""   # our source_id or QB GL row key


# ---------------------------------------------------------------------------
# Comparison report
# ---------------------------------------------------------------------------

@dataclass
class ComparisonReport:
    """Multi-grain comparison result."""
    from_date: date
    to_date: date
    material_diffs: list[DiffRecord] = field(default_factory=list)
    immaterial_diffs: list[DiffRecord] = field(default_factory=list)
    unmatched_ours: list[UnmatchedTransaction] = field(default_factory=list)
    unmatched_qb: list[UnmatchedTransaction] = field(default_factory=list)
    matched_count: int = 0
    readiness: Optional[ReadinessReport] = None
    errors: list[str] = field(default_factory=list)
    pending_categorization_count: int = 0  # Bug 5: number of items still pending categorization

    def total_material(self) -> int:
        return len(self.material_diffs)

    def to_dict(self) -> dict:
        def _ut(u: UnmatchedTransaction) -> dict:
            return {
                "side": u.side,
                "date": str(u.txn_date),
                "amount": str(u.amount),
                "description": u.description,
                "account": u.account,
                "source_id": u.source_id,
            }

        return {
            "from_date": str(self.from_date),
            "to_date": str(self.to_date),
            "material_diff_count": len(self.material_diffs),
            "material_diffs": [_diff_to_dict(d) for d in self.material_diffs],
            "immaterial_diffs": [_diff_to_dict(d) for d in self.immaterial_diffs],
            "unmatched_ours": [_ut(u) for u in self.unmatched_ours],
            "unmatched_qb": [_ut(u) for u in self.unmatched_qb],
            "matched_count": self.matched_count,
            "pending_categorization_count": self.pending_categorization_count,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

_CONFIDENCE_DIR = "migration-confidence"
_DIFFERENCES_FILE = "differences.json"


def _confidence_dir(entity: Entity) -> Path:
    d = entity.reports_dir / _CONFIDENCE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_differences(entity: Entity) -> list[DiffRecord]:
    """Load persisted differences (returns [] when file is absent)."""
    path = _confidence_dir(entity) / _DIFFERENCES_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [_dict_to_diff(d) for d in data]
    except (json.JSONDecodeError, KeyError, ValueError):
        return []


def save_differences(entity: Entity, diffs: list[DiffRecord]) -> None:
    """Atomic write of differences.json."""
    conf_dir = _confidence_dir(entity)
    path = conf_dir / _DIFFERENCES_FILE
    tmp = path.with_suffix(".json.tmp")
    content = json.dumps([_diff_to_dict(d) for d in diffs], indent=2, sort_keys=False) + "\n"
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _merge_differences(
    existing: list[DiffRecord],
    fresh: list[DiffRecord],
    abs_t: Decimal,
    pct_t: Decimal,
) -> list[DiffRecord]:
    """Merge fresh diffs with existing, preserving accepted/fixed dispositions.

    Rules:
    - If fresh diff key matches an existing one:
      - Keep existing status/note/category unless delta materially changed.
      - If delta materially changed and status was accepted/fixed → re-open.
    - Fresh diffs not in existing are added with status=open.
    - Existing diffs whose key no longer appears in fresh are dropped
      (the condition no longer exists).
    """
    existing_by_key: dict[str, DiffRecord] = {d.key: d for d in existing}
    result: list[DiffRecord] = []

    for fresh_d in fresh:
        if fresh_d.key in existing_by_key:
            prev = existing_by_key[fresh_d.key]
            # Check if delta changed materially
            delta_change = abs(fresh_d.delta - prev.delta)
            delta_is_material_change = _is_material(
                delta_change, fresh_d.ours, fresh_d.reference, abs_t, pct_t
            )
            if delta_is_material_change and prev.status in ("accepted", "fixed"):
                # Re-open with updated numbers
                merged = DiffRecord(
                    key=fresh_d.key,
                    grain=fresh_d.grain,
                    account=fresh_d.account,
                    ours=fresh_d.ours,
                    reference=fresh_d.reference,
                    delta=fresh_d.delta,
                    category=prev.category,   # keep categorization
                    status="open",
                    note=prev.note,
                    re_opened=True,
                )
            else:
                # Keep disposition; update numbers
                merged = DiffRecord(
                    key=fresh_d.key,
                    grain=fresh_d.grain,
                    account=fresh_d.account,
                    ours=fresh_d.ours,
                    reference=fresh_d.reference,
                    delta=fresh_d.delta,
                    category=prev.category,
                    status=prev.status,
                    note=prev.note,
                    re_opened=prev.re_opened,
                )
            result.append(merged)
        else:
            result.append(fresh_d)

    return result


# ---------------------------------------------------------------------------
# QB account name → beancount account mapper (stateless helper)
# ---------------------------------------------------------------------------

def _map_qb_name(name: str, qb_type_map: dict[str, str]) -> str:
    """Map a QB name to beancount account using type map."""
    qtype = qb_type_map.get(name, "Expenses")
    return map_qb_account(name, qtype)


# ---------------------------------------------------------------------------
# Transaction matching helpers
# ---------------------------------------------------------------------------

_MATCH_DATE_WINDOW = 3  # ±3 days


def _token_overlap(a: str, b: str) -> float:
    """Fraction of tokens in common between two normalized description strings."""
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    return len(intersection) / max(len(tokens_a), len(tokens_b))


def _norm_first_segment(raw: str) -> str:
    """Normalize the first ';'-delimited segment of a description.

    Both our narrations and QB GL descriptions can carry '; …' suffixes
    with extra tokens (e.g. '; Merchant name: …' or '; PCS SVC; …').
    Taking only the first segment before splitting into tokens dramatically
    improves match rates without sacrificing precision.
    """
    first = raw.split(";")[0].strip()
    return normalize_description(first)


def _txns_match(
    our_date: date,
    our_amount: Decimal,
    our_norm_desc: str,
    qb_date: date,
    qb_amount: Decimal,
    qb_norm_desc: str,
) -> bool:
    """Return True if two transactions match across date±3, amount, and description."""
    date_diff = abs((our_date - qb_date).days)
    if date_diff > _MATCH_DATE_WINDOW:
        return False
    if our_amount != qb_amount:
        return False
    overlap = _token_overlap(our_norm_desc, qb_norm_desc)
    return overlap >= 0.5


# ---------------------------------------------------------------------------
# Staging pending loader (for timing heuristic)
# ---------------------------------------------------------------------------

def _load_staging_pending(entity: Entity) -> list[dict]:
    """Load pending staging entries.

    Checks both ``pending.json`` (legacy test fixtures) and the real-world
    ``pending-categorization.json`` produced by the importer.
    """
    for filename in ("pending-categorization.json", "pending.json"):
        pending_path = entity.staging_dir / filename
        if pending_path.exists():
            try:
                data = json.loads(pending_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return list(data.values())
                return []
            except (json.JSONDecodeError, OSError):
                continue
    return []


def _pending_sum_near_period_end(entity: Entity, period_end: date, window_days: int = 3) -> Decimal:
    """Sum of pending transaction amounts that were staged near the period end."""
    staged = _load_staging_pending(entity)
    total = Decimal("0.00")
    cutoff_start = period_end - timedelta(days=window_days)
    for txn in staged:
        raw_date = str(txn.get("date") or "")
        try:
            txn_date = date.fromisoformat(raw_date[:10])
        except (ValueError, TypeError):
            continue
        if cutoff_start <= txn_date <= period_end:
            raw_amount = txn.get("amount") or "0"
            try:
                amount = Decimal(str(raw_amount)).quantize(Decimal("0.01"))
                total += amount
            except Exception:
                pass
    return total


# ---------------------------------------------------------------------------
# Core comparison grains
# ---------------------------------------------------------------------------

def _compare_pnl(
    entity: Entity,
    qb_folder: Path,
    from_date: date,
    to_date: date,
    qb_type_map: dict[str, str],
    abs_t: Decimal,
    pct_t: Decimal,
    readiness: Optional[ReadinessReport] = None,
    pending_count: int = 0,
) -> tuple[list[DiffRecord], list[DiffRecord]]:
    """Compare P&L category totals.

    Returns (material_diffs, immaterial_diffs).
    Uses the readiness-report profit_and_loss slot to select the correct file
    (period-matched by date range), falling back to _find_report_file only when
    no readiness report is provided.
    """
    # Our P&L
    try:
        our_pnl = stmt_pnl(entity.path, from_date, to_date)
    except Exception:
        return [], []

    # QB P&L — use inventory slot to get the period-correct file (Bug 1 fix).
    pnl_path: Optional[Path] = None
    if readiness is not None:
        for slot in readiness.slots:
            if slot.report_key == "profit_and_loss" and slot.status == "present" and slot.file:
                pnl_path = Path(slot.file)
                break
    if pnl_path is None:
        pnl_files = _find_report_file(qb_folder, "profit_and_loss")
        if not pnl_files:
            return [], []
        pnl_path = pnl_files[0]
    try:
        qb_pnl = parse_profit_and_loss(pnl_path)
    except Exception:
        return [], []

    # Build our totals per beancount account
    our_totals: dict[str, Decimal] = {}
    for section in our_pnl.sections:
        for row in section.get("rows", []):
            label = row.get("label", "")
            # Convert rendered label back to account form (reverse _render_account)
            account = label.replace(" › ", ":")
            amount = row.get("amount", Decimal("0"))
            if amount is not None:
                our_totals[account] = Decimal(str(amount))

    # Reset collision registry for consistent mapping
    _reset_collision_registry()

    # Build QB totals per leaf row (skip subtotals). Leaf rows under a
    # non-top-level section carry the section as their parent path — QB's
    # chart names them "Parent:Leaf" (e.g. "Accounting & Professional
    # Fees:Accounting Fees"), and mapping the bare leaf name would diverge
    # from how our books (and import-opening) name the same account.
    qb_totals: dict[str, Decimal] = {}
    _top_sections = {"income", "expenses", "other income", "other expenses",
                     "other income and expenses", "cost of goods sold"}
    parent: Optional[str] = None
    for row in qb_pnl.rows:
        if row.row_type == "section":
            name = row.name.strip()
            parent = None if name.lower() in _top_sections else name
            continue
        if row.row_type == "subtotal":
            name = row.name.strip()
            if name.startswith("Total for ") and parent and name[len("Total for "):].strip() == parent:
                parent = None
            continue
        if row.row_type == "leaf":
            try:
                qb_name = f"{parent}:{row.name}" if parent else row.name
                bc_account = _map_qb_name(qb_name, qb_type_map)
                qb_totals[bc_account] = qb_totals.get(bc_account, Decimal("0.00")) + row.amount
            except Exception:
                pass

    material: list[DiffRecord] = []
    immaterial: list[DiffRecord] = []

    all_accounts = set(our_totals.keys()) | set(qb_totals.keys())
    for account in sorted(all_accounts):
        ours = our_totals.get(account, Decimal("0.00"))
        ref = qb_totals.get(account, Decimal("0.00"))
        delta = ours - ref
        if delta == Decimal("0.00"):
            continue

        # Categorization heuristic (Bug 5 fix: annotate pending when ours=0)
        note = ""
        category = "uncategorized"
        if account not in our_totals:
            # Reference has a category we haven't posted to yet.
            if pending_count > 0:
                # Transactions still pending categorization may account for this.
                category = "uncategorized"
                note = f"{pending_count} transactions still pending categorization"
            else:
                category = "judgment-mapping"
        elif account not in qb_totals:
            category = "judgment-mapping"

        d = _diff_record("pnl", account, ours, ref, category=category, note=note)

        if _is_material(delta, ours, ref, abs_t, pct_t):
            material.append(d)
        else:
            immaterial.append(d)

    return material, immaterial


def _compare_bs(
    entity: Entity,
    qb_folder: Path,
    to_date: date,
    qb_type_map: dict[str, str],
    abs_t: Decimal,
    pct_t: Decimal,
    readiness: Optional[ReadinessReport] = None,
) -> tuple[list[DiffRecord], list[DiffRecord]]:
    """Compare balance sheet account balances as of period end.

    Uses the balance_sheet_comparison slot (period-end BS) when available
    via the readiness report — this is the correct reference for the as-of
    period-end comparison (Bug 2 fix).
    """
    try:
        our_bs = stmt_bs(entity.path, to_date)
    except Exception:
        return [], []

    # Use the balance_sheet_comparison slot (period-end BS) when available via
    # the readiness report.  Fall back to balance_sheet slot if the comparison
    # slot is absent, then fall back to _find_report_file.  (Bug 2 fix.)
    bs_path: Optional[Path] = None
    if readiness is not None:
        for preferred_key in ("balance_sheet_comparison", "balance_sheet"):
            for slot in readiness.slots:
                if slot.report_key == preferred_key and slot.status == "present" and slot.file:
                    bs_path = Path(slot.file)
                    break
            if bs_path is not None:
                break
    if bs_path is None:
        bs_files = _find_report_file(qb_folder, "balance_sheet", multiple=True)
        if not bs_files:
            return [], []
        # Pick the last (most recent) BS file alphabetically as the best guess
        bs_path = bs_files[-1]
    try:
        qb_bs = parse_balance_sheet(bs_path)
    except Exception:
        return [], []

    # Our balances by account
    our_balances: dict[str, Decimal] = {}
    for section in our_bs.sections:
        for row in section.get("rows", []):
            label = row.get("label", "")
            account = label.replace(" › ", ":")
            amount = row.get("amount")
            if amount is not None:
                our_balances[account] = Decimal(str(amount))

    _reset_collision_registry()

    # QB leaf balances
    qb_balances: dict[str, Decimal] = {}
    for row in qb_bs.rows:
        if row.row_type == "leaf":
            try:
                bc_account = _map_qb_name(row.name, qb_type_map)
                qb_balances[bc_account] = row.amount
            except Exception:
                pass

    material: list[DiffRecord] = []
    immaterial: list[DiffRecord] = []

    all_accounts = set(our_balances.keys()) | set(qb_balances.keys())
    for account in sorted(all_accounts):
        ours = our_balances.get(account, Decimal("0.00"))
        ref = qb_balances.get(account, Decimal("0.00"))
        delta = ours - ref
        if delta == Decimal("0.00"):
            continue

        category = "uncategorized"
        if account not in our_balances or account not in qb_balances:
            category = "judgment-mapping"

        d = _diff_record("bs", account, ours, ref, category=category)

        if _is_material(delta, ours, ref, abs_t, pct_t):
            material.append(d)
        else:
            immaterial.append(d)

    return material, immaterial


def _compare_tb(
    entity: Entity,
    qb_folder: Path,
    to_date: date,
    abs_t: Decimal,
    pct_t: Decimal,
) -> tuple[list[DiffRecord], list[DiffRecord]]:
    """Compare trial balance totals (debit/credit sums)."""
    # Only compare if QB has a cash-basis TB
    tb_files = _find_report_file(qb_folder, "trial_balance")
    if not tb_files:
        return [], []
    try:
        qb_tb = parse_trial_balance(tb_files[0])
    except Exception:
        return [], []

    if qb_tb.basis != "cash":
        # Cannot compare against accrual TB — this is a blocking condition
        return [], []

    try:
        our_tb = stmt_tb(entity.path, to_date)
    except Exception:
        return [], []

    our_debits = our_tb.totals.get("total_debits", Decimal("0.00"))
    our_credits = our_tb.totals.get("total_credits", Decimal("0.00"))

    qb_debits = sum(e.debit for e in qb_tb.entries)
    qb_credits = sum(e.credit for e in qb_tb.entries)

    material: list[DiffRecord] = []
    immaterial: list[DiffRecord] = []

    for label, ours, ref in [
        ("total_debits", our_debits, qb_debits),
        ("total_credits", our_credits, qb_credits),
    ]:
        delta = ours - ref
        if delta == Decimal("0.00"):
            continue
        d = _diff_record("tb", label, ours, ref)
        if _is_material(delta, ours, ref, abs_t, pct_t):
            material.append(d)
        else:
            immaterial.append(d)

    return material, immaterial


def _compare_gl_counts(
    entity: Entity,
    qb_folder: Path,
    from_date: date,
    to_date: date,
    qb_type_map: dict[str, str],
    abs_t: Decimal,
    pct_t: Decimal,
    readiness: Optional[ReadinessReport] = None,
) -> tuple[list[DiffRecord], list[DiffRecord]]:
    """Compare transaction counts per account between our GL and QB GL.

    Uses the readiness-report general_ledger slot for period-correct file
    selection (Bug 1 fix).
    """
    # Use inventory slot to get the period-correct GL file.
    gl_path: Optional[Path] = None
    if readiness is not None:
        for slot in readiness.slots:
            if slot.report_key == "general_ledger" and slot.status == "present" and slot.file:
                gl_path = Path(slot.file)
                break
    if gl_path is None:
        gl_files = _find_report_file(qb_folder, "general_ledger")
        if not gl_files:
            return [], []
        gl_path = gl_files[0]
    try:
        qb_gl = parse_general_ledger(gl_path)
    except Exception:
        return [], []

    # QB counts per mapped account, filtered to period
    qb_counts: dict[str, int] = {}
    for txn in qb_gl.transactions:
        if not (from_date <= txn.txn_date <= to_date):
            continue
        try:
            bc_account = _map_qb_name(txn.account, qb_type_map)
            qb_counts[bc_account] = qb_counts.get(bc_account, 0) + 1
        except Exception:
            pass

    # Our counts per account from GL
    try:
        our_gl = stmt_gl(entity.path, from_date, to_date)
    except Exception:
        return [], []

    our_counts: dict[str, int] = {}
    for section in our_gl.sections:
        account = section.get("label", "").replace(" › ", ":")
        our_counts[account] = len(section.get("rows", []))

    material: list[DiffRecord] = []
    immaterial: list[DiffRecord] = []

    all_accounts = set(our_counts.keys()) | set(qb_counts.keys())
    for account in sorted(all_accounts):
        ours_n = Decimal(str(our_counts.get(account, 0)))
        ref_n = Decimal(str(qb_counts.get(account, 0)))
        delta = ours_n - ref_n
        if delta == Decimal("0.00"):
            continue

        category = "uncategorized"
        if account not in our_counts or account not in qb_counts:
            category = "judgment-mapping"

        d = _diff_record("gl-count", account, ours_n, ref_n, category=category)

        # Materiality on count diffs: use abs threshold only (pct of count is odd)
        if abs(delta) >= abs_t:
            material.append(d)
        else:
            immaterial.append(d)

    return material, immaterial


def _is_bank_card_account(qb_account_name: str, qb_type_map: dict[str, str]) -> bool:
    """Return True when the QB account is a bank or credit-card account."""
    qtype = qb_type_map.get(qb_account_name, "").strip().lower()
    return qtype in _BANK_CARD_QB_TYPES


_OPENING_NARRATION_MARKERS = frozenset([
    "opening balance",
    "opening balances",
    "import-source",  # meta key value used in narration context
])

_TRANSFER_CLEARING_ACCOUNT = "Assets:Transfers-Clearing"


def _is_opening_entry(narration: str, payee: Optional[str]) -> bool:
    """Return True when the entry is an opening-balances entry (should be excluded)."""
    text = ((payee or "") + " " + (narration or "")).lower()
    return any(m in text for m in _OPENING_NARRATION_MARKERS)


def _find_unmatched(
    entity: Entity,
    qb_folder: Path,
    from_date: date,
    to_date: date,
    qb_type_map: dict[str, str],
    readiness: Optional[ReadinessReport] = None,
) -> tuple[list[UnmatchedTransaction], list[UnmatchedTransaction], int]:
    """Find unmatched transactions in both directions.

    Returns (unmatched_ours, unmatched_qb, matched_count).

    Bug-3 fixes applied:
    - QB GL: only match rows in bank/card account sections (each real-world
      transaction appears exactly once in the bank/card section; P&L sections
      would duplicate every transaction).
    - Our side: only match bank/card postings (Assets:Bank:* and
      Liabilities:CreditCard:*); exclude opening entries and transfers-clearing.
    - Descriptions: normalize the first ';'-delimited segment on both sides
      so that '; Merchant name: …' and '; PCS SVC; …' suffixes don't dilute
      the token overlap score.
    - Amounts: QB GL bank-account amounts already carry the correct sign (debit
      from bank = negative for bank account); our bank postings use the same
      sign convention for asset accounts, so no sign flip is needed.
    """
    # Use inventory slot to get the period-correct GL file (Bug 1 fix).
    gl_path: Optional[Path] = None
    if readiness is not None:
        for slot in readiness.slots:
            if slot.report_key == "general_ledger" and slot.status == "present" and slot.file:
                gl_path = Path(slot.file)
                break
    if gl_path is None:
        gl_files = _find_report_file(qb_folder, "general_ledger")
        if not gl_files:
            return [], [], 0
        gl_path = gl_files[0]

    try:
        qb_gl = parse_general_ledger(gl_path)
    except Exception:
        return [], [], 0

    # QB transactions in period — BANK/CARD SECTIONS ONLY (Bug 3 fix).
    # Each real-world transaction appears in exactly one bank/card section in the
    # QB GL; using only those sections avoids double-counting from P&L sections.
    qb_txns = [
        t for t in qb_gl.transactions
        if from_date <= t.txn_date <= to_date
        and _is_bank_card_account(t.account, qb_type_map)
    ]

    # Our transactions from the cache GL — bank/card postings only (Bug 3 fix).
    try:
        from .reports.cache import open_cache, iter_postings
        conn = open_cache(entity.path)
        try:
            our_txns_raw = list(iter_postings(conn, from_date=from_date, to_date=to_date))
        finally:
            conn.close()
    except Exception:
        return [], [], 0

    # Build our list from bank/card postings only, excluding:
    # - Opening-balance entries (not real transactions)
    # - Assets:Transfers-Clearing postings (internal transfers, not external txns)
    #
    # Sign normalization for credit card accounts (Bug 3 fix):
    # In beancount, credit card charges are negative (liability credit) and
    # payments are positive (liability debit).  QB GL credit card sections show
    # charges as POSITIVE and payments as NEGATIVE — the exact opposite.  We
    # negate our Liabilities:CreditCard:* amounts so both sides use QB convention
    # (positive = charge, negative = payment) during matching.
    our_list: list[tuple[date, Decimal, str, str, str]] = []
    for entry_date, narration, payee, account, amount, currency in our_txns_raw:
        # Skip opening entries
        if _is_opening_entry(narration or "", payee):
            continue
        # Skip transfers-clearing (these are our internal books offsets)
        if account == _TRANSFER_CLEARING_ACCOUNT:
            continue
        # Only include bank (Assets:Bank:*), credit card (Liabilities:CreditCard:*),
        # and CSV-sourced card accounts (Assets:*-Card-CSV, Assets:*-Card-*).
        # The last category covers accounts like Assets:Amex-Card-CSV where credit
        # card transactions imported from CSV files are stored.
        is_bank = account.startswith("Assets:Bank:")
        is_cc_liability = account.startswith("Liabilities:CreditCard:")
        is_card_csv = (
            account.startswith("Assets:")
            and not account.startswith("Assets:Bank:")
            and not account.startswith("Assets:Transfers")
            and ("Card" in account or "Amex" in account or "CSV" in account)
        )
        if not (is_bank or is_cc_liability or is_card_csv):
            continue
        d = entry_date if isinstance(entry_date, date) else date.fromisoformat(str(entry_date)[:10])
        # Build description: payee first (short merchant name), then narration.
        # Use first ';'-segment of whichever is more informative.
        if payee:
            raw_desc = payee
        else:
            raw_desc = narration or ""
        nd = _norm_first_segment(raw_desc)
        # Normalize sign for credit card / card-CSV accounts to match QB GL convention.
        # QB GL credit card sections: charges=positive, payments=negative.
        # Our beancount CC postings: charges=negative, payments=positive.
        # Our CSV card imports (Assets:*-Card-CSV): charges=negative (asset decreases).
        # Negate both to align with QB convention.
        is_cc_like = is_cc_liability or is_card_csv
        match_amount = -amount if is_cc_like else amount
        our_list.append((d, match_amount, nd, account, ""))

    # Build QB list: first-segment normalized description from name+desc (Bug 3 fix).
    qb_list: list[tuple[date, Decimal, str, str]] = []
    for txn in qb_txns:
        # QB name is often the merchant name (short); description has extra tokens.
        # Use name if non-empty (it's the cleanest identifier), else fall back to desc.
        if txn.name:
            raw_desc = txn.name
        else:
            raw_desc = txn.description or ""
        nd = _norm_first_segment(raw_desc)
        qb_list.append((txn.txn_date, txn.amount, nd, txn.account))

    # Matching: greedy O(n*m) — fine for typical statement sizes
    our_matched = [False] * len(our_list)
    qb_matched = [False] * len(qb_list)
    matched_count = 0

    for i, (od, oa, ond, oacc, _osid) in enumerate(our_list):
        for j, (qd, qa, qnd, qacc) in enumerate(qb_list):
            if qb_matched[j]:
                continue
            if _txns_match(od, oa, ond, qd, qa, qnd):
                our_matched[i] = True
                qb_matched[j] = True
                matched_count += 1
                break

    unmatched_ours: list[UnmatchedTransaction] = []
    for i, (od, oa, ond, oacc, _osid) in enumerate(our_list):
        if not our_matched[i]:
            unmatched_ours.append(UnmatchedTransaction(
                side="ours",
                txn_date=od,
                amount=oa,
                description=ond,
                account=oacc,
            ))

    unmatched_qb: list[UnmatchedTransaction] = []
    for j, (qd, qa, qnd, qacc) in enumerate(qb_list):
        if not qb_matched[j]:
            unmatched_qb.append(UnmatchedTransaction(
                side="qb",
                txn_date=qd,
                amount=qa,
                description=qnd,
                account=qacc,
            ))

    return unmatched_ours, unmatched_qb, matched_count


# ---------------------------------------------------------------------------
# Report-file locator
# ---------------------------------------------------------------------------

def _find_report_file(folder: Path, report_type: str, multiple: bool = False) -> list[Path]:
    """Find QuickBooks export files of the given report type in the folder."""
    from .quickbooks import _read_export_rows, detect_report_type
    results = []
    for f in iter_export_files(folder):
        try:
            rows = _read_export_rows(f)
            if detect_report_type(rows) == report_type:
                results.append(f)
                if not multiple:
                    return results
        except Exception:
            pass
    return results


# ---------------------------------------------------------------------------
# Main comparison entry point
# ---------------------------------------------------------------------------

def compare_period(
    entity: Entity,
    qb_folder: Path,
    from_date: date,
    to_date: date,
) -> ComparisonReport:
    """Multi-grain generated-vs-reference comparison.

    Compares:
    (a) P&L category totals
    (b) Balance-sheet account balances as of period end
    (c) Trial-balance totals (when a cash-basis TB exists)
    (d) Transaction counts per account (GL grain)
    (e) Unmatched transactions both directions
    """
    report = ComparisonReport(from_date=from_date, to_date=to_date)

    # Readiness check — inventory assigns files to slots by period semantics,
    # so every subsequent grain can use slot.file to get the period-correct CSV
    # rather than relying on alphabetical sort order (Bug 1 fix).
    readiness = inventory(qb_folder)
    report.readiness = readiness

    abs_t, pct_t = _materiality_thresholds(entity)

    # Build QB type map from the chart-of-accounts slot (collision registry reset
    # here is the canonical reset; each comparison grain resets it before mapping).
    qb_type_map: dict[str, str] = {}
    from .quickbooks import parse_chart_of_accounts, _read_export_rows, detect_report_type
    _reset_collision_registry()
    coa_slot = next((s for s in readiness.slots
                     if s.report_key == "chart_of_accounts" and s.status == "present"), None)
    if coa_slot and coa_slot.file:
        try:
            coa_accounts = parse_chart_of_accounts(Path(coa_slot.file))
            qb_type_map = {a.name: a.account_type for a in coa_accounts}
        except Exception:
            pass
    if not qb_type_map:
        # Fallback: scan folder directly
        for f in iter_export_files(qb_folder):
            try:
                rows = _read_export_rows(f)
                if detect_report_type(rows) == "chart_of_accounts":
                    coa_accounts = parse_chart_of_accounts(f)
                    qb_type_map = {a.name: a.account_type for a in coa_accounts}
                    break
            except Exception:
                pass

    # Pending-categorization count for Bug 5 annotation.
    pending_items = _load_staging_pending(entity)
    pending_count = len(pending_items)

    # (a) P&L
    try:
        pm, pi = _compare_pnl(
            entity, qb_folder, from_date, to_date, qb_type_map, abs_t, pct_t,
            readiness=readiness, pending_count=pending_count,
        )
        report.material_diffs.extend(pm)
        report.immaterial_diffs.extend(pi)
    except Exception as e:
        report.errors.append(f"P&L comparison error: {e}")

    # (b) Balance sheet
    try:
        bm, bi = _compare_bs(
            entity, qb_folder, to_date, qb_type_map, abs_t, pct_t,
            readiness=readiness,
        )
        report.material_diffs.extend(bm)
        report.immaterial_diffs.extend(bi)
    except Exception as e:
        report.errors.append(f"Balance sheet comparison error: {e}")

    # (c) Trial balance
    try:
        tm, ti = _compare_tb(entity, qb_folder, to_date, abs_t, pct_t)
        report.material_diffs.extend(tm)
        report.immaterial_diffs.extend(ti)
    except Exception as e:
        report.errors.append(f"Trial balance comparison error: {e}")

    # (d) GL transaction counts
    try:
        gm, gi = _compare_gl_counts(
            entity, qb_folder, from_date, to_date, qb_type_map, abs_t, pct_t,
            readiness=readiness,
        )
        report.material_diffs.extend(gm)
        report.immaterial_diffs.extend(gi)
    except Exception as e:
        report.errors.append(f"GL count comparison error: {e}")

    # (e) Unmatched transactions
    try:
        uo, uq, matched = _find_unmatched(
            entity, qb_folder, from_date, to_date, qb_type_map,
            readiness=readiness,
        )
        report.unmatched_ours = uo
        report.unmatched_qb = uq
        report.matched_count = matched
    except Exception as e:
        report.errors.append(f"Transaction matching error: {e}")

    # Apply timing-pending heuristic: for any material diff whose delta equals
    # the sum of staged pendings near period end, reclassify as timing-pending.
    pending_sum = _pending_sum_near_period_end(entity, to_date)
    for d in report.material_diffs:
        if d.category == "uncategorized" and abs(abs(d.delta) - abs(pending_sum)) <= Decimal("0.01"):
            if pending_sum != Decimal("0.00"):
                d.category = "timing-pending"

    # Annotate the report with pending count so callers can surface it.
    report.pending_categorization_count = pending_count

    # Persist differences (merge with existing to preserve dispositions)
    try:
        existing = load_differences(entity)
        all_fresh = report.material_diffs + report.immaterial_diffs
        merged = _merge_differences(existing, all_fresh, abs_t, pct_t)
        save_differences(entity, merged)
        # Update report's diffs to reflect merged dispositions
        merged_by_key = {d.key: d for d in merged}
        report.material_diffs = [merged_by_key[d.key] for d in report.material_diffs if d.key in merged_by_key]
        report.immaterial_diffs = [merged_by_key[d.key] for d in report.immaterial_diffs if d.key in merged_by_key]
    except Exception:
        pass  # Best-effort persistence

    return report


# ---------------------------------------------------------------------------
# Spot-audit sample
# ---------------------------------------------------------------------------

def _spot_audit_sample(
    entity: Entity,
    from_date: date,
    to_date: date,
    target_count: int = 10,
) -> list[dict]:
    """Select a deterministic sample of matched/categorized transactions.

    Seeded by sorted source IDs; takes every Nth to yield ~target_count items.
    Each sample item includes: date, counterparty, amount, our_category.

    Bug 4 fix: excludes opening-balance entries, import-source entries, and
    Assets:Transfers-Clearing postings so that amount is never 0.00 and the
    sample reflects only real merchant transactions.  Only entries with at
    least one Expenses: or Income: posting are included (meaning they have
    been categorized), matching the intent of 'sampled MATCHED merchant
    transactions only'.
    """
    try:
        from .reports.cache import open_cache, iter_postings
        conn = open_cache(entity.path)
        try:
            rows = list(iter_postings(conn, from_date=from_date, to_date=to_date))
        finally:
            conn.close()
    except Exception:
        return []

    # Group by entry (date+narration+payee).  We collect all postings per entry
    # then keep only entries that have at least one categorized posting.
    entry_data: dict[tuple, dict] = {}
    entry_has_category: dict[tuple, bool] = {}

    for entry_date, narration, payee, account, amount, currency in rows:
        # Exclude opening entries
        if _is_opening_entry(narration or "", payee):
            continue
        # Exclude transfers-clearing postings
        if account == _TRANSFER_CLEARING_ACCOUNT:
            continue
        d = entry_date if isinstance(entry_date, date) else date.fromisoformat(str(entry_date)[:10])
        key = (str(d), narration or "", payee or "")
        if key not in entry_data:
            entry_data[key] = {
                "date": str(d),
                "counterparty": payee or narration or "",
                "amount": Decimal("0.00"),
                "our_category": account,
            }
            entry_has_category[key] = False
        # Pick the most "interesting" account (expense/income over asset)
        if account.startswith("Expenses:") or account.startswith("Income:"):
            entry_data[key]["our_category"] = account
            entry_data[key]["amount"] = abs(amount)
            entry_has_category[key] = True

    # Only sample entries that have a categorized posting (Bug 4: no 0.00 amounts)
    sample_pool_all = [
        v for k, v in entry_data.items() if entry_has_category.get(k, False)
    ]
    # Sort by (date, counterparty) for determinism
    sample_pool = sorted(sample_pool_all, key=lambda x: (x["date"], x["counterparty"]))

    if not sample_pool:
        return []

    # Deterministic step sampling
    n = len(sample_pool)
    step = max(1, n // target_count)
    sample = sample_pool[::step][:target_count]

    # Format amounts as strings
    result = []
    for item in sample:
        result.append({
            "date": item["date"],
            "counterparty": item["counterparty"],
            "amount": str(item["amount"]),
            "our_category": item["our_category"],
        })
    return result


# ---------------------------------------------------------------------------
# Source coverage matrix
# ---------------------------------------------------------------------------

def _source_coverage(entity: Entity, from_date: date, to_date: date) -> list[dict]:
    """Build a coverage matrix: declared sources × date coverage."""
    sources = entity.entity_config.get("declared_sources", [])
    if not sources:
        # Infer from the ledger accounts
        try:
            from .reports.cache import open_cache, list_accounts
            conn = open_cache(entity.path)
            try:
                accounts = list_accounts(conn)
            finally:
                conn.close()
            sources = [
                {"name": a["name"], "type": "inferred"}
                for a in accounts
                if a["name"].startswith("Assets:") or a["name"].startswith("Liabilities:")
            ]
        except Exception:
            sources = []

    coverage = []
    for src in sources:
        src_name = src if isinstance(src, str) else src.get("name", str(src))
        coverage.append({
            "source": src_name,
            "requested_from": str(from_date),
            "requested_to": str(to_date),
            "status": "declared",
        })
    return coverage


# ---------------------------------------------------------------------------
# Confidence package
# ---------------------------------------------------------------------------

def confidence_package(
    entity: Entity,
    qb_folder: Path,
    from_date: date,
    to_date: date,
) -> dict:
    """Generate and persist the migration confidence package.

    Writes to <entity>/reports/migration-confidence/:
      - comparison.json        (full comparison report)
      - differences.json       (persisted differences)
      - confidence-summary.md  (plain-English summary)

    Returns the full package dict.
    """
    readiness = inventory(qb_folder)
    comparison = compare_period(entity, qb_folder, from_date, to_date)
    diffs = load_differences(entity)
    spot_sample = _spot_audit_sample(entity, from_date, to_date)
    source_cov = _source_coverage(entity, from_date, to_date)

    # Readiness recap
    readiness_recap = {
        "ready": readiness.is_ready(),
        "slots": [
            {
                "label": s.label,
                "status": s.status,
                "file": s.file,
                "block_reason": s.block_reason,
            }
            for s in readiness.slots
        ],
        "ambiguous_files": readiness.ambiguous_files,
    }

    # Match summary per grain
    grain_summary: dict[str, dict] = {}
    for d in diffs:
        grain = d.grain
        if grain not in grain_summary:
            grain_summary[grain] = {
                "total_diffs": 0,
                "open": 0,
                "accepted": 0,
                "fixed": 0,
                "uncategorized": 0,
            }
        grain_summary[grain]["total_diffs"] += 1
        grain_summary[grain][d.status] = grain_summary[grain].get(d.status, 0) + 1
        if d.category == "uncategorized":
            grain_summary[grain]["uncategorized"] += 1

    # Group diffs by category and status
    by_category: dict[str, list[dict]] = {}
    for d in diffs:
        by_category.setdefault(d.category, []).append(_diff_to_dict(d))

    # Missing source data items
    missing_source = [_diff_to_dict(d) for d in diffs if d.category == "missing-source-data"]

    # Blocking items
    blocking = [s for s in readiness.slots if s.status == "blocked"]

    package = {
        "entity": entity.name,
        "period": {"from": str(from_date), "to": str(to_date)},
        "generated_at": _now_iso(),
        "readiness_recap": readiness_recap,
        "source_coverage": source_cov,
        "grain_summary": grain_summary,
        "match_summary": {
            "matched_transactions": comparison.matched_count,
            "unmatched_ours": len(comparison.unmatched_ours),
            "unmatched_qb": len(comparison.unmatched_qb),
        },
        "total_material_diffs": len([d for d in diffs if d.grain not in ("unmatched-ours", "unmatched-qb")]),
        "differences_by_category": by_category,
        "missing_source_data": missing_source,
        "spot_audit_sample": spot_sample,
        "blocking_items": [{"label": s.label, "reason": s.block_reason} for s in blocking],
        "errors": comparison.errors,
    }

    # Persist comparison.json
    conf_dir = _confidence_dir(entity)
    _atomic_write(conf_dir / "comparison.json", json.dumps(comparison.to_dict(), indent=2) + "\n")

    # Persist confidence-summary.md
    md = _render_confidence_markdown(package)
    _atomic_write(conf_dir / "confidence-summary.md", md)

    return package


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _render_confidence_markdown(package: dict) -> str:
    """Render a plain-English summary of the confidence package (no jargon)."""
    lines: list[str] = []
    lines.append(f"# Migration Confidence Report: {package['entity']}")
    lines.append("")
    period = package["period"]
    lines.append(f"**Period:** {period['from']} through {period['to']}")
    lines.append(f"**Generated:** {package['generated_at']}")
    lines.append("")

    # Readiness
    recap = package["readiness_recap"]
    ready_label = "Ready" if recap["ready"] else "Not fully ready"
    lines.append(f"## Reference Files Status: {ready_label}")
    lines.append("")
    for slot in recap["slots"]:
        icon = {"present": "OK", "missing": "MISSING", "blocked": "BLOCKED"}.get(slot["status"], "?")
        lines.append(f"- [{icon}] {slot['label']}")
        if slot.get("block_reason"):
            lines.append(f"  - Action needed: {slot['block_reason']}")
    if recap.get("ambiguous_files"):
        lines.append("")
        lines.append("Unrecognized files in the folder:")
        for f in recap["ambiguous_files"]:
            lines.append(f"  - {f}")
    lines.append("")

    # Blocking items
    if package.get("blocking_items"):
        lines.append("## Blocking Issues (must resolve before certifying)")
        lines.append("")
        for b in package["blocking_items"]:
            lines.append(f"- **{b['label']}**: {b['reason']}")
        lines.append("")

    # Match summary
    ms = package["match_summary"]
    lines.append("## Transaction Matching Summary")
    lines.append("")
    lines.append(f"- Matched transactions: {ms['matched_transactions']}")
    lines.append(f"- In our records only: {ms['unmatched_ours']}")
    lines.append(f"- In reference only: {ms['unmatched_qb']}")
    lines.append("")

    # Differences
    total_diffs = package["total_material_diffs"]
    lines.append(f"## Differences Found: {total_diffs} material")
    lines.append("")
    by_cat = package["differences_by_category"]
    if by_cat:
        lines.append("### By Category")
        lines.append("")
        for cat, cat_diffs in sorted(by_cat.items()):
            open_count = sum(1 for d in cat_diffs if d["status"] == "open")
            accepted = sum(1 for d in cat_diffs if d["status"] == "accepted")
            fixed = sum(1 for d in cat_diffs if d["status"] == "fixed")
            lines.append(f"**{cat.replace('-', ' ').title()}** ({len(cat_diffs)} total, {open_count} open, {accepted} accepted, {fixed} fixed)")
            for d in cat_diffs[:5]:  # Show up to 5 per category
                lines.append(f"  - {d['account']}: ours {d['ours']}, reference {d['reference']}, difference {d['delta']}")
            if len(cat_diffs) > 5:
                lines.append(f"  - ... and {len(cat_diffs) - 5} more")
            lines.append("")
    else:
        lines.append("No material differences found.")
        lines.append("")

    # Spot audit sample
    sample = package.get("spot_audit_sample", [])
    if sample:
        lines.append("## Independent Spot-Audit Sample")
        lines.append("")
        lines.append("The following transactions were categorized by our system. Please verify a few independently.")
        lines.append("")
        lines.append("| Date | Counterparty | Amount | Our Category |")
        lines.append("|------|-------------|--------|--------------|")
        for item in sample:
            cat = item["our_category"].replace(":", " > ")
            lines.append(f"| {item['date']} | {item['counterparty']} | {item['amount']} | {cat} |")
        lines.append("")

    # Open questions
    lines.append("## Open Questions")
    lines.append("")
    lines.append("- Materiality thresholds (abs >= $25.00 or >= 1% of account total) are defaults pending owner sign-off.")
    lines.append("- Use `books compare categorize-diff` to assign categories to uncategorized differences.")
    lines.append("- Use `books compare accept-diff` to mark differences as accepted.")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backtest orchestration
# ---------------------------------------------------------------------------

_PASSTHROUGH_CATEGORY = "Expenses:Uncategorized"


def _passthrough_categorizer(entity: Entity) -> object:
    """Build a categorizer that routes everything to pending-categorization.

    Checks entity_config.category_rules for exact counterparty/description-prefix
    matches. Everything else goes to the pending-categorization list (returns
    empty string to trigger the pending-cat path in the importer).
    """
    rules: list[dict] = entity.entity_config.get("category_rules", [])

    def categorize(txn: dict) -> tuple[str, str]:
        desc = str(txn.get("description") or "").strip()
        payee = str(txn.get("accountName") or "").strip()
        name = str(txn.get("name") or payee).strip()

        for rule in rules:
            match_type = rule.get("match", "exact")
            pattern = str(rule.get("pattern", ""))
            account = str(rule.get("account", ""))
            if not account or not pattern:
                continue
            if match_type == "exact":
                if desc.upper() == pattern.upper() or name.upper() == pattern.upper():
                    return account, "rule-match"
            elif match_type == "prefix":
                if desc.upper().startswith(pattern.upper()) or name.upper().startswith(pattern.upper()):
                    return account, "rule-prefix"

        # Default: route to pending-categorization (return empty account)
        return "", "uncategorized"

    return categorize


def backtest_run(
    entity_path: Path,
    qb_folder: Path,
    from_date: date,
    to_date: date,
    *,
    skip_fetch: bool = False,
    banksync_json_files: Optional[list[Path]] = None,
    partial: bool = False,
    print_fn=print,
) -> int:
    """Orchestrate the full backtest chain.

    Chain:
    1. Ingest: load BankSync data (live or from saved JSON) + import
    2. Regenerate cache
    3. Generate statements
    4. compare_period
    5. confidence_package

    Returns 0 on success, non-zero when there are blocking items (unless --partial).
    """
    print_fn(f"Backtest: {entity_path.name}  {from_date} through {to_date}")
    print_fn("")

    # --- Load entity ---
    try:
        entity = load_entity(entity_path)
    except FileNotFoundError as exc:
        print_fn(f"ERROR: {exc}")
        return 1

    # --- Step 1: Ingest ---
    print_fn("Step 1: Ingesting transactions...")

    normalized_txns: list[dict] = []

    if banksync_json_files:
        for bsf in banksync_json_files:
            print_fn(f"  Loading BankSync data from {bsf.name}")
            try:
                data = json.loads(bsf.read_text(encoding="utf-8"))
                # Expect the collect_bank_data output shape
                banks = data.get("banks", [])
                for bank in banks:
                    for account in bank.get("accounts", []):
                        txns = account.get("transactions", [])
                        normalized_txns.extend(txns)
                        print_fn(f"    {len(txns)} transactions from {account.get('account', {}).get('accountName', 'unknown')}")
            except Exception as exc:
                print_fn(f"  WARNING: Could not load {bsf}: {exc}")

    elif not skip_fetch:
        print_fn("  No --banksync-json provided and --skip-fetch not set.")
        print_fn("  Skipping live fetch (no BankSync credentials configured for backtest).")

    else:
        print_fn("  --skip-fetch set; skipping data fetch.")

    # Import normalized transactions
    if normalized_txns:
        import datetime as _dt
        session_id = f"backtest-{from_date}-{to_date}"
        from .ledger.importer import import_transactions
        categorizer = _passthrough_categorizer(entity)
        try:
            result = import_transactions(
                entity,
                normalized_txns,
                session_id=session_id,
                categorizer=categorizer,
                ts=None,
            )
            print_fn(f"  Imported: {result.new_entries} entries, {result.pending_categorization} pending categorization, {result.skipped_duplicate} duplicates skipped")
        except Exception as exc:
            print_fn(f"  WARNING: Import error: {exc}")

    # --- Step 2: Regenerate cache ---
    print_fn("")
    print_fn("Step 2: Rebuilding financial reports...")
    from .reports.cache import regenerate
    # Create an empty ledger if none exists yet (all transactions may have gone
    # to pending-categorization when no category rules are configured)
    if not entity.books_path.exists():
        _empty_ledger = (
            'option "title" "Books"\n'
            'option "operating_currency" "USD"\n'
            'option "inferred_tolerance_default" "USD:0.005"\n'
        )
        entity.books_path.write_text(_empty_ledger, encoding="utf-8")
        print_fn("  Note: No transactions were auto-posted (all pending categorization).")
        print_fn("  Created empty ledger placeholder.")
    cache_result = regenerate(entity.path)
    if not cache_result:
        print_fn(f"  WARNING: Cache regeneration failed: {cache_result.error_message}")
        if not partial:
            print_fn("  Use --partial to continue anyway.")
            return 1
        print_fn("  Continuing with --partial flag...")
    else:
        print_fn("  Reports rebuilt successfully.")

    # --- Step 3: Statements ---
    print_fn("")
    print_fn("Step 3: Generating financial statements...")
    try:
        pnl = stmt_pnl(entity.path, from_date, to_date)
        net_income = pnl.totals.get("net_income", Decimal("0.00"))
        print_fn(f"  P&L net income: {net_income:,.2f}")
        bs = stmt_bs(entity.path, to_date)
        total_assets = bs.totals.get("total_assets", Decimal("0.00"))
        print_fn(f"  Balance sheet total assets: {total_assets:,.2f}")
    except Exception as exc:
        print_fn(f"  WARNING: Statement generation error: {exc}")

    # --- Step 4: Comparison ---
    print_fn("")
    print_fn("Step 4: Comparing against QuickBooks reference...")
    try:
        comparison = compare_period(entity, qb_folder, from_date, to_date)
        print_fn(f"  Material differences: {len(comparison.material_diffs)}")
        print_fn(f"  Unmatched (ours / QB): {len(comparison.unmatched_ours)} / {len(comparison.unmatched_qb)}")
        print_fn(f"  Matched transactions: {comparison.matched_count}")
    except Exception as exc:
        print_fn(f"  WARNING: Comparison error: {exc}")
        comparison = None

    # --- Step 5: Confidence package ---
    print_fn("")
    print_fn("Step 5: Generating migration confidence package...")
    try:
        package = confidence_package(entity, qb_folder, from_date, to_date)
        conf_dir = _confidence_dir(entity)
        print_fn(f"  Confidence package written to: {conf_dir}")
        blocking = package.get("blocking_items", [])
        if blocking:
            print_fn("")
            print_fn("BLOCKING ITEMS — cannot certify migration:")
            for b in blocking:
                print_fn(f"  - {b['label']}: {b['reason']}")
    except Exception as exc:
        print_fn(f"  WARNING: Confidence package error: {exc}")
        blocking = []
        package = {}

    # --- Summary ---
    print_fn("")
    print_fn("=" * 60)
    print_fn("Backtest complete.")
    if comparison:
        print_fn(f"  Material differences: {len(comparison.material_diffs)}")
    diffs_path = entity.reports_dir / _CONFIDENCE_DIR / _DIFFERENCES_FILE
    if diffs_path.exists():
        print_fn(f"  Differences file: {diffs_path}")
    md_path = entity.reports_dir / _CONFIDENCE_DIR / "confidence-summary.md"
    if md_path.exists():
        print_fn(f"  Summary report:   {md_path}")

    # Check for accrual TB blocking
    if package:
        blocking = package.get("blocking_items", [])
        if blocking and not partial:
            print_fn("")
            print_fn("Cannot certify migration: the following blocking items must be resolved first:")
            for b in blocking:
                print_fn(f"  - {b['label']}: {b['reason']}")
            return 2

    return 0


# ---------------------------------------------------------------------------
# Diff mutation helpers (for CLI)
# ---------------------------------------------------------------------------

def categorize_diff(entity: Entity, key: str, category: str, note: str = "") -> bool:
    """Set the category and note for a difference by key. Returns True if found."""
    if category not in _VALID_CATEGORIES:
        raise ValueError(f"Invalid category {category!r}. Valid: {sorted(_VALID_CATEGORIES)}")
    diffs = load_differences(entity)
    for d in diffs:
        if d.key == key:
            d.category = category
            if note:
                d.note = note
            save_differences(entity, diffs)
            return True
    return False


def accept_diff(entity: Entity, key: str, note: str = "") -> bool:
    """Mark a difference as accepted. Returns True if found."""
    diffs = load_differences(entity)
    for d in diffs:
        if d.key == key:
            d.status = "accepted"
            if note:
                d.note = note
            save_differences(entity, diffs)
            return True
    return False


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def add_parser(subparsers: Any) -> None:
    """Register `backtest` and `compare` subcommands onto *subparsers*."""

    # ---- backtest -----------------------------------------------------------
    bt_parser = subparsers.add_parser("backtest", help="Run backtest and comparison")
    bt_sub = bt_parser.add_subparsers(dest="backtest_command", required=True)

    bt_run = bt_sub.add_parser(
        "run",
        help="Orchestrate full backtest: ingest → ledger → statements → compare → confidence",
    )
    bt_run.add_argument("--entity", required=True, type=Path, dest="entity_path",
                        help="Path to entity directory")
    bt_run.add_argument("--qb-folder", required=True, type=Path, dest="qb_folder",
                        help="Folder containing QuickBooks CSV/XLSX exports")
    bt_run.add_argument("--from", required=True, dest="from_date",
                        help="Start date YYYY-MM-DD")
    bt_run.add_argument("--to", required=True, dest="to_date",
                        help="End date YYYY-MM-DD")
    bt_run.add_argument("--skip-fetch", action="store_true", dest="skip_fetch",
                        help="Skip live BankSync fetch")
    bt_run.add_argument("--banksync-json", nargs="*", type=Path, dest="banksync_json",
                        default=None,
                        help="Pre-saved BankSync JSON files (collect_bank_data output shape)")
    bt_run.add_argument("--partial", action="store_true",
                        help="Continue and produce what it can even if blocking items exist")

    # ---- compare ------------------------------------------------------------
    cmp_parser = subparsers.add_parser("compare", help="QuickBooks comparison tools")
    cmp_sub = cmp_parser.add_subparsers(dest="compare_command", required=True)

    # compare period
    period_p = cmp_sub.add_parser(
        "period",
        help="Compare generated books against QB reference for a period",
    )
    period_p.add_argument("--entity", required=True, type=Path, dest="entity_path")
    period_p.add_argument("--qb-folder", required=True, type=Path, dest="qb_folder")
    period_p.add_argument("--from", required=True, dest="from_date")
    period_p.add_argument("--to", required=True, dest="to_date")
    period_p.add_argument("--json", action="store_true", dest="as_json",
                          help="Output as JSON")

    # compare categorize-diff
    cat_p = cmp_sub.add_parser(
        "categorize-diff",
        help="Assign a category to a material difference",
    )
    cat_p.add_argument("--entity", required=True, type=Path, dest="entity_path")
    cat_p.add_argument("--key", required=True, dest="diff_key",
                       help="Difference key (e.g. pnl:Income:Revenue:Consulting)")
    cat_p.add_argument("--category", required=True, dest="category",
                       choices=sorted(_VALID_CATEGORIES))
    cat_p.add_argument("--note", default="", dest="note")

    # compare accept-diff
    accept_p = cmp_sub.add_parser(
        "accept-diff",
        help="Mark a difference as accepted",
    )
    accept_p.add_argument("--entity", required=True, type=Path, dest="entity_path")
    accept_p.add_argument("--key", required=True, dest="diff_key")
    accept_p.add_argument("--note", default="", dest="note")


def run(args: Any) -> int:
    """Dispatch compare/backtest subcommands."""
    cmd = getattr(args, "command", None)

    if cmd == "backtest":
        bc = getattr(args, "backtest_command", None)
        if bc == "run":
            try:
                entity_path = Path(args.entity_path).resolve()
                qb_folder = Path(args.qb_folder).resolve()
                from_date = date.fromisoformat(args.from_date)
                to_date = date.fromisoformat(args.to_date)
            except (ValueError, TypeError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

            banksync_files = [Path(f) for f in (args.banksync_json or [])]

            return backtest_run(
                entity_path=entity_path,
                qb_folder=qb_folder,
                from_date=from_date,
                to_date=to_date,
                skip_fetch=getattr(args, "skip_fetch", False),
                banksync_json_files=banksync_files or None,
                partial=getattr(args, "partial", False),
            )

        print(f"Unknown backtest command: {bc}", file=sys.stderr)
        return 2

    if cmd == "compare":
        cc = getattr(args, "compare_command", None)

        if cc == "period":
            try:
                entity = load_entity(args.entity_path)
                from_date = date.fromisoformat(args.from_date)
                to_date = date.fromisoformat(args.to_date)
                qb_folder = Path(args.qb_folder).resolve()
            except (ValueError, FileNotFoundError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

            report = compare_period(entity, qb_folder, from_date, to_date)

            if getattr(args, "as_json", False):
                print(json.dumps(report.to_dict(), indent=2))
            else:
                d = report.to_dict()
                print(f"Comparison: {from_date} through {to_date}")
                print(f"  Material differences: {d['material_diff_count']}")
                print(f"  Matched transactions: {d['matched_count']}")
                print(f"  Unmatched (ours / QB): {len(d['unmatched_ours'])} / {len(d['unmatched_qb'])}")
                if d["material_diffs"]:
                    print("")
                    print("  Material differences:")
                    for diff in d["material_diffs"]:
                        print(f"    [{diff['category']}] {diff['account']}: delta={diff['delta']}")
                if d["errors"]:
                    print("")
                    print("  Errors:")
                    for e in d["errors"]:
                        print(f"    - {e}")
            return 0

        if cc == "categorize-diff":
            try:
                entity = load_entity(args.entity_path)
            except FileNotFoundError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

            try:
                found = categorize_diff(entity, args.diff_key, args.category, args.note)
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

            if found:
                print(f"Updated difference {args.diff_key!r}: category={args.category!r}")
            else:
                print(f"No difference found with key {args.diff_key!r}", file=sys.stderr)
                return 1
            return 0

        if cc == "accept-diff":
            try:
                entity = load_entity(args.entity_path)
            except FileNotFoundError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

            found = accept_diff(entity, args.diff_key, args.note)
            if found:
                print(f"Accepted difference {args.diff_key!r}")
            else:
                print(f"No difference found with key {args.diff_key!r}", file=sys.stderr)
                return 1
            return 0

        print(f"Unknown compare command: {cc}", file=sys.stderr)
        return 2

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2
