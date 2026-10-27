"""Statement of cash flows, indirect method.

This is the financial statement.  It is not the cash forecast in
``bookkeeping.cashflow``.

The engine starts from the period net income of the profit and loss statement and
adds the effect on cash of every change in a balance sheet account.  All math uses
``Decimal``.  The sign rule is one rule for every account: a ledger balance is
debit-positive, so the cash effect of a change in any non-cash account is
``-(ending balance - beginning balance)``.  Journal-level adjustments remove noncash asset funding and classify explicitly linked
capital-payable settlements before activity subtotals are calculated.

Groups
------
Each account is placed in a group from its ``subtype``, the same value that
``books accrual accounts`` reports (starter chart name, registered use, then
account-name prefix; a cash book without accrual records falls back to the
prefix).

``cash``               bank, cash.  These accounts are the cash of the statement.
``operating``          receivable, payable, prepaid, deferred revenue, customer
                       deposits, accrued liabilities, credit card, and the
                       depreciation add-back (change in accumulated depreciation).
``investing``          fixed assets.
``financing``          equity accounts, and liabilities whose name says loan, note,
                       mortgage, debt or line of credit.
``needs_classification``  any other account with a change.  Examples: a payment
                       processor balance, a transfer or suspense account, an
                       unnamed other liability, retained earnings.  The engine does
                       not guess.  The amounts still count in the net change in
                       cash, so the statement ties, and the group is shown.

Credit cards are working capital, not cash: a card charge is an expense in net
income and an increase in the card balance, so it appears as an operating line.

Ledger integrity check
----------------------
Net income comes from the same postings as the balances, so the check is not an
outside proof.  It catches an unbalanced or damaged ledger.  It cannot catch an
account in the wrong group, because a wrong group moves an amount between two groups
and leaves the net change the same.  Beginning cash + net change in cash must equal
the bank and cash accounts at the end date, and that total is read again straight
from the postings.  Both comparisons use
cents.  When one differs, ``reconciliation.ties`` is ``false``, the differences are
shown, and the command exits with code 1.  An asset account named like cash (for
example ``Assets:Checking``) that is not a bank or cash account gives a warning.

Definition of cash: bank and cash accounts.  Credit card balances are liabilities in
operating working capital.  (The accrual cash view counts a card charge as paid on
the charge date, so the two reports differ on purpose.)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from .cache import get_account_balance, open_cache
from .statements import _SEP, _account_balances_as_of, _compute_pnl, journal_coverage_view
from .structure import SCHEMA_VERSION

ZERO = Decimal("0.00")

CASH_DEFINITION = "Cash means bank and cash accounts. Credit card balances are liabilities in operating working capital."

_CASH_SUBTYPES = {"bank", "cash"}
_OPERATING_SUBTYPES = ("receivable", "prepaid", "payable", "accrued_liabilities", "deferred_revenue",
                       "customer_deposits", "credit_card")
_FINANCING_NAME = re.compile(
    r"(?<![A-Za-z])(loans?|notes?|mortgages?|debt|line[-_ ]?of[-_ ]?credit|loc)(?![A-Za-z])", re.IGNORECASE)
# Names that contain a debt word but are working capital: vendor credit notes, interest
# and collection fees.
_NOT_DEBT_NAME = re.compile(r"(credit|debit)[-_ ]?notes?|interest|collection", re.IGNORECASE)
_CASH_LIKE_NAME = re.compile(r"(?<![A-Za-z])(cash|checking|savings|bank|petty)(?![A-Za-z])", re.IGNORECASE)
_OPENING_NAME = re.compile(r"opening", re.IGNORECASE)
_RETAINED_NAME = re.compile(r"retained|prior[-_ ]?year|prior[-_ ]?period", re.IGNORECASE)

GROUP_LABELS = {
    "operating": ("Operating activities", "Net cash from operating activities"),
    "investing": ("Investing activities", "Net cash from investing activities"),
    "financing": ("Financing activities", "Net cash from financing activities"),
    "needs_classification": ("Needs classification", "Total needing classification"),
}


def _money(value: Decimal) -> str:
    return str(Decimal(value).quantize(ZERO))


def classify_accounts(conn: Any, names: set[str]) -> dict[str, str]:
    """Return ``{account: subtype}`` using the engine's own account subtype rules."""
    from ..accrual.accounts import _subtype, _usage_subtypes
    from ..accrual.reports import _records

    records = {table: _records(conn, table) for table in ("items", "schedules")}
    usage = _usage_subtypes(records)
    subtypes = {name: _subtype(name, usage) for name in names}
    for name, subtype in subtypes.items():
        # A nested contra account such as Assets:Fixed:Equipment:Accumulated-Depreciation is
        # accumulated depreciation, not a fixed asset.
        if (subtype == "fixed_asset" and name not in usage
                and any(part.lower().startswith("accumulated") for part in name.split(":")[1:])):
            subtypes[name] = "accumulated_depreciation"
    return subtypes


def _is_debt_name(account: str) -> bool:
    """True when the account name says loan, note, mortgage, debt or line of credit."""
    leaf = account.split(":", 1)[-1]
    return bool(_FINANCING_NAME.search(leaf)) and not _NOT_DEBT_NAME.search(leaf)


def classify(account: str, subtype: str) -> tuple[str, str]:
    """Return ``(group, reason)`` for one balance sheet account."""
    if subtype in _CASH_SUBTYPES:
        return "cash", f"{subtype} account"
    if subtype in {"payable", "other_liability"} and _is_debt_name(account):
        return "financing", "debt account (the debt name wins over the payable prefix)"
    if subtype == "accumulated_depreciation":
        return "operating", "depreciation add-back"
    if subtype in _OPERATING_SUBTYPES:
        return "operating", f"{subtype.replace('_', ' ')} is working capital"
    if subtype == "fixed_asset":
        return "investing", "fixed asset"
    if subtype == "equity":
        if _OPENING_NAME.search(account.split(":", 1)[-1]):
            return "needs_classification", "opening balance equity is a starting point, not a financing flow"
        if _RETAINED_NAME.search(account.split(":", 1)[-1]):
            return "needs_classification", "retained earnings or prior-period equity moves for a reason the engine cannot see"
        return "financing", "equity account"
    if subtype == "other_liability":
        return "needs_classification", "other liability: operating or financing is not known"
    if subtype == "other_asset":
        return "needs_classification", "other asset: operating, investing or a clearing balance is not known"
    return "needs_classification", f"{subtype} has no cash flow group"


def _is_balance_sheet(account: str) -> bool:
    return account.split(":")[0] in {"Assets", "Liabilities", "Equity"}


@dataclass
class CashflowResult:
    from_date: date
    to_date: date
    lines: list[dict]
    totals: dict[str, Decimal]
    reconciliation: dict[str, Any]
    needs_classification: list[dict]
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict] = field(default_factory=list)
    kind: str = "cashflow"

    @property
    def sections(self) -> list[dict]:
        """The same ``sections`` shape as the other statements, for existing renderers."""
        out: list[dict] = []
        for line in self.lines:
            if line["type"] == "section":
                out.append({"label": line["label"], "rows": [], "key": line["key"]})
            elif line["type"] in {"account", "computed"} and out:
                out[-1]["rows"].append({"label": line["label"], "amount": Decimal(line["amount"]),
                                        "indent": line.get("indent", 1)})
            elif line["type"] == "subtotal" and out and line.get("section") == out[-1]["key"]:
                out[-1]["total"] = Decimal(line["amount"])
                out[-1]["total_label"] = line["label"]
        return [{k: v for k, v in s.items() if k != "key"} for s in out]

    def payload(self) -> dict:
        return {
            "kind": self.kind,
            "schema_version": SCHEMA_VERSION,
            "method": "indirect",
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "sections": [{**s, "rows": [{**r, "amount": str(r["amount"])} for r in s["rows"]],
                          "total": str(s["total"])} for s in self.sections],
            "totals": {k: str(v) for k, v in self.totals.items()},
            "reconciliation": self.reconciliation,
            "needs_classification": self.needs_classification,
            "warnings": self.warnings,
            "noncash_events": self.meta.get("noncash_events", []),
            "cash_definition": CASH_DEFINITION,
            "structure": {"version": SCHEMA_VERSION, "lines": self.lines},
            "meta": self.meta,
        }

    def to_json(self) -> str:
        return json.dumps(self.payload(), indent=2, sort_keys=False)

    def to_text(self) -> str:
        head = f"Statement of Cash Flows  {self.from_date}  through  {self.to_date}"
        out = [head, "=" * len(head)]
        if self.meta.get("basis"):
            out.append(f"Accounting basis: {self.meta['basis']}")
        coverage = self.meta.get("coverage")
        if coverage:
            out.append(f"Activity cutover: {coverage['cutover_date']}, opening balances as of {coverage['source_as_of']}")
        out.append("Method: indirect")
        out.append(CASH_DEFINITION)
        for issue in self.meta.get("unresolved_issues", []):
            message = issue.get("message") or issue.get("code", "Unresolved issue").replace("_", " ")
            out.append(f"  - {message}")
        for line in self.lines:
            if line["type"] == "section":
                out += ["", line["label"], "-" * 40]
            elif "amount" in line:
                pad = "  " * line.get("indent", 0)
                if line["type"] in {"account", "computed"} and line.get("section"):
                    pad = "  " * max(line.get("indent", 1), 1)
                out.append(f"{pad}{line['label']:<{42 - len(pad)}} {Decimal(line['amount']):>14,.2f}")
        if self.meta.get("noncash_events"):
            out += ["", "Noncash investing and financing activities", "-" * 40]
            for event in self.meta["noncash_events"]:
                out.append(f"  {event['date']}  {event['amount']}  {event['description']}")
        rec = self.reconciliation
        out += ["", "Ledger integrity check", "-" * 40,
                f"  Beginning cash {rec['beginning_cash']} + net change {rec['net_change_in_cash']} = {rec['ending_cash_per_statement']}",
                f"  Bank and cash accounts at {self.to_date}: {rec['cash_accounts_total']}"]
        if rec["ties"]:
            out.append("  The check passed: the statement agrees with the bank and cash account balances.")
        else:
            out.append(f"  DIFFERENCE: {rec['difference']} (statement) and {rec['cash_accounts_difference']} (accounts). "
                       "The ledger does not pass the integrity check.")
        for warning in self.warnings:
            out.append(f"  WARNING: {warning['message']}")
        if self.needs_classification:
            out += ["", "Needs classification: these accounts changed and the engine does not know their group.",
                    "The amounts are in the net change in cash. Classify each account to move it."]
            for item in self.needs_classification:
                out.append(f"  - {item['label']}: {item['cash_effect']} ({item['reason']})")
        return "\n".join(out)


def build_cash_flow(
    from_date: date,
    to_date: date,
    net_income: Decimal,
    begin: dict[str, Decimal],
    end: dict[str, Decimal],
    subtypes: dict[str, str],
    meta: Optional[dict[str, Any]] = None,
    independent_ending_cash: Optional[Decimal] = None,
    balance_adjustments: Optional[dict[str, Decimal]] = None,
    activity_adjustments: Optional[list[dict]] = None,
) -> CashflowResult:
    """Pure builder: balances in, statement out.  No I/O."""
    groups: dict[str, list[tuple[str, Decimal, str, str]]] = {k: [] for k in GROUP_LABELS}
    cash_accounts: list[dict] = []
    beginning_cash = ending_cash = ZERO
    for account in sorted(set(begin) | set(end)):
        if not _is_balance_sheet(account):
            if account.split(":")[0] not in {"Income", "Expenses"}:
                effect = -(end.get(account, ZERO) - begin.get(account, ZERO))
                if effect:
                    groups["needs_classification"].append(
                        (account, effect, "unknown",
                         "account type is not an asset, liability, equity, income or expense"))
            continue
        before, after = begin.get(account, ZERO), end.get(account, ZERO)
        group, reason = classify(account, subtypes.get(account, "other_asset"))
        if group == "cash":
            beginning_cash += before
            ending_cash += after
            cash_accounts.append({"account": account, "beginning": _money(before), "ending": _money(after)})
            continue
        effect = -(after - before) + (balance_adjustments or {}).get(account, ZERO)
        if effect:
            groups[group].append((account, effect, subtypes.get(account, ""), reason))

    for adjustment in activity_adjustments or []:
        groups[adjustment["group"]].append((adjustment["account"], adjustment["amount"], "settlement", adjustment["reason"]))

    def rendered(account: str) -> str:
        return account.replace(":", _SEP)

    lines: list[dict] = []
    subtotals: dict[str, Decimal] = {k: ZERO for k in GROUP_LABELS}
    needs: list[dict] = []
    for group, (label, total_label) in GROUP_LABELS.items():
        if group == "needs_classification" and not groups[group]:
            continue
        lines.append({"type": "section", "key": group, "label": label, "indent": 0})
        total = ZERO
        if group == "operating":
            lines.append({"type": "computed", "key": "net_income", "label": "Net income", "amount": _money(net_income),
                          "indent": 1, "section": group})
            total += net_income
        for account, effect, subtype, reason in groups[group]:
            if subtype == "settlement":
                text = reason
            elif subtype == "accumulated_depreciation":
                text = f"Depreciation and amortization ({rendered(account)})"
            else:
                text = f"Change in {rendered(account)}"
            lines.append({"type": "account", "key": account, "label": text, "account": account,
                          "amount": _money(effect), "indent": 1, "section": group})
            total += effect
            if group == "needs_classification":
                needs.append({"account": account, "label": text, "subtype": subtype, "cash_effect": _money(effect),
                              "reason": reason})
        lines.append({"type": "subtotal", "key": f"net_cash_{group}" if group != "needs_classification" else "needs_classification",
                      "label": total_label, "amount": _money(total), "indent": 0, "section": group})
        subtotals[group] = total

    net_change = sum(subtotals.values(), ZERO)
    ending_per_statement = beginning_cash + net_change
    # Quantize before comparing, so a sub-cent residue never reads as "Difference: 0.00".
    cents = Decimal("0.01")
    cross_check = ending_cash if independent_ending_cash is None else independent_ending_cash
    difference = ending_per_statement.quantize(cents) - ending_cash.quantize(cents)
    cross_difference = ending_cash.quantize(cents) - cross_check.quantize(cents)
    warnings: list[dict] = []
    for account in sorted(set(begin) | set(end)):
        if (account.split(":")[0] == "Assets" and _CASH_LIKE_NAME.search(account.split(":", 1)[-1])
                and classify(account, subtypes.get(account, "other_asset"))[0] == "needs_classification"
                and (begin.get(account, ZERO) or end.get(account, ZERO))):
            warnings.append({"code": "possible_cash_account", "account": account,
                             "message": f"{rendered(account)} looks like a cash account but is not a bank or cash account, "
                                        "so it is not counted as cash."})
    lines.append({"type": "total", "key": "net_change_in_cash", "label": "Net change in cash",
                  "amount": _money(net_change), "indent": 0})
    lines.append({"type": "computed", "key": "beginning_cash", "label": "Cash at beginning of period",
                  "amount": _money(beginning_cash), "indent": 0})
    lines.append({"type": "total", "key": "ending_cash", "label": "Cash at end of period",
                  "amount": _money(ending_per_statement), "indent": 0})

    totals = {
        "net_income": net_income,
        "net_cash_operating": subtotals["operating"],
        "net_cash_investing": subtotals["investing"],
        "net_cash_financing": subtotals["financing"],
        "needs_classification": subtotals["needs_classification"],
        "net_change_in_cash": net_change,
        "beginning_cash": beginning_cash,
        "ending_cash": ending_per_statement,
    }
    reconciliation = {
        "beginning_cash": _money(beginning_cash),
        "net_change_in_cash": _money(net_change),
        "ending_cash_per_statement": _money(ending_per_statement),
        "balance_sheet_cash": _money(ending_cash),
        "check": "ledger_integrity",
        "difference": _money(difference),
        "cash_accounts_total": _money(cross_check),
        "cash_accounts_difference": _money(cross_difference),
        "ties": difference == 0 and cross_difference == 0,
        "cash_accounts": cash_accounts,
    }
    return CashflowResult(from_date, to_date, lines, totals, reconciliation, needs, dict(meta or {}), warnings)


_OPENING_SOURCES = {"quickbooks-opening", "accrual-opening", "statement-opening"}


def opening_entries(conn: Any, from_date: date, to_date: date) -> list[dict]:
    """Imported opening-balance entries dated in [from_date, to_date].

    An entry is an opening entry when its ``import-source`` is ``quickbooks-opening``,
    ``accrual-opening`` or ``statement-opening``, or its ``accrual-opening`` metadata is ``true``.  A ledger
    cache without entry metadata is matched by the QuickBooks opening ``source_id``.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
    meta_column = "metadata_json" if "metadata_json" in columns else "'[]'"
    found = []
    # A statement opening that a later entry reverses (it moved to another day) is not an opening, and
    # neither is its reversal: the pair nets to zero as ordinary entries.
    reversed_ids = set()
    if meta_column != "'[]'":
        for (raw,) in conn.execute("SELECT metadata_json FROM entries WHERE metadata_json LIKE '%reverses%'"):
            try:
                decoded = json.loads(raw or "[]")
                pairs = dict(decoded.items() if isinstance(decoded, dict) else decoded)
            except (TypeError, ValueError):
                continue
            if pairs.get("reverses"):
                reversed_ids.add(pairs["reverses"])
    rows = conn.execute(
        f"SELECT id, date, source_id, {meta_column} FROM entries WHERE date >= ? AND date <= ? ORDER BY id",
        (from_date.isoformat(), to_date.isoformat()),
    ).fetchall()
    for entry_id, entry_date, source_id, raw in rows:
        try:
            decoded = json.loads(raw or "[]")
            meta = dict(decoded.items() if isinstance(decoded, dict) else decoded)
        except (TypeError, ValueError):
            meta = {}
        if meta.get("import-source") == "statement-opening" and ("reverses" in meta or source_id in reversed_ids):
            continue
        if (meta.get("import-source") in _OPENING_SOURCES or meta.get("accrual-opening") == "true"
                or str(source_id or "").startswith("quickbooks-opening-")):
            found.append({"id": entry_id, "date": date.fromisoformat(entry_date)})
    return found


def _opening_balances(conn: Any, entry_ids: list[int]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for entry_id in entry_ids:
        for account, amount in conn.execute("SELECT account, amount FROM postings WHERE entry_id = ?", (entry_id,)):
            totals[account] = totals.get(account, ZERO) + Decimal(amount)
    return totals


def _journal_adjustments(conn: Any, from_date: date, to_date: date, subtypes: dict[str, str],
                         opening_ids: set[int]) -> tuple[dict, list, list, list]:
    """Remove evidenced noncash asset funding; classify linked capital settlements.

    Balance deltas alone cannot distinguish equipment acquired on account from an
    operating payable. Journal legs establish the noncash event; item allocations
    establish which later payment settles it. No shared-payable FIFO is invented.
    """
    from .cashview import _load_entries, _cumulative, _cents, _dec
    from ..accrual.reports import _records
    from collections import defaultdict

    entries = _load_entries(conn, to_date)
    sources = {entry["source_id"]: entry for entry in entries if entry["source_id"]}
    offsets: dict[str, Decimal] = defaultdict(lambda: ZERO)
    flows, events, issues = [], [], []
    for entry in entries:
        if entry["id"] in opening_ids or not (from_date.isoformat() <= entry["date"] <= to_date.isoformat()):
            continue
        postings = entry["postings"]
        # A mixed cash purchase/expense needs explicit purpose allocation; a
        # wholly noncash mixed bill is still unambiguous about its capital part.
        mixed_cash = any(subtypes.get(a) in _CASH_SUBTYPES and n for a, n in postings)
        mixed_income = any(a.startswith(("Income:", "Expenses:")) and n for a, n in postings)
        if mixed_cash and mixed_income and any(subtypes.get(a) == "fixed_asset" and n for a, n in postings):
            # The journal establishes total cash but cannot say whether the
            # cash or the payable funded its expense versus capital portion.
            # Keep that actual cash visible without inventing group subtotals.
            for account, amount in postings:
                if _is_balance_sheet(account) and subtypes.get(account) not in _CASH_SUBTYPES:
                    offsets[account] += amount
            income_expense = sum((n for a, n in postings if a.startswith(("Income:", "Expenses:"))), ZERO)
            cash_change = sum((n for a, n in postings if subtypes.get(a) in _CASH_SUBTYPES), ZERO)
            flows.extend([
                {"group": "operating", "account": "mixed-purpose-income:" + str(entry["id"]),
                 "amount": income_expense, "reason": "Remove income or expense awaiting cash-purpose classification"},
                {"group": "needs_classification", "account": "mixed-purpose-cash:" + str(entry["id"]),
                 "amount": cash_change, "reason": "Mixed capital and operating payment needs an explicit cash-purpose split"},
            ])
            issues.append({"code": "cash_flow_mixed_purchase_allocation", "source_id": entry["source_id"],
                           "message": "A mixed capital and operating cash entry needs an explicit cash-purpose split: "
                                      + (entry["narration"] or entry["date"])})
            continue
        for direction in (Decimal(1), Decimal(-1)):
            assets = [(a, n) for a, n in postings if subtypes.get(a) == "fixed_asset" and n * direction > 0]
            funding = [(a, n) for a, n in postings if _is_balance_sheet(a)
                       and subtypes.get(a) not in _CASH_SUBTYPES | {"fixed_asset"} and n * direction < 0]
            remaining = sum((abs(n) for _, n in funding), ZERO)
            for asset, amount in assets:
                allocated = min(abs(amount), remaining)
                if not allocated:
                    continue
                offsets[asset] += direction * allocated
                left = allocated
                for index, (account, value) in enumerate(funding):
                    part = min(left, abs(value))
                    offsets[account] -= direction * part
                    funding[index] = (account, value + direction * part)
                    left -= part
                    if not left:
                        break
                remaining -= allocated
                events.append({"date": entry["date"], "source_id": entry["source_id"], "account": asset,
                               "amount": _money(direction * allocated),
                               "description": entry["narration"] or "Noncash asset transfer"})

    items = {item["id"]: item for item in _records(conn, "items")}
    settled: dict[str, int] = defaultdict(int)
    allocations = _records(conn, "allocations")
    for allocation in sorted(allocations, key=lambda row: (row.get("date", ""), row["id"])):
        item = items.get(allocation.get("item_id"))
        if allocation.get("kind") != "settlement" or not item or item.get("kind") != "payable":
            continue
        origin = item.get("origin", {})
        if origin.get("mode") == "opening":
            # Opening items have no supported purchase-purpose detail.
            continue
        if origin.get("lines"):
            weights = [(row["account"], _cents(row["amount"])) for row in origin["lines"]]
        elif origin.get("counter_account"):
            weights = [(origin["counter_account"], _cents(item["original_amount"]))]
        else:
            source = sources.get(origin.get("source_id"), {})
            weights = [(a, _cents(n)) for a, n in source.get("postings", []) if a != item["account"] and n > 0]
        if not weights or any(n < 0 for _, n in weights) or sum(n for _, n in weights) != _cents(item["original_amount"]):
            continue
        amount = _cents(allocation["amount"])
        before = _cumulative(weights, settled[item["id"]])
        after = _cumulative(weights, settled[item["id"]] + amount)
        settled[item["id"]] += amount
        if not (from_date.isoformat() <= allocation.get("date", "") <= to_date.isoformat()):
            continue
        # Only bank/cash settlements are cash flows. A card-funded payment
        # remains noncash until a bank/cash payment is explicitly linked.
        if subtypes.get(allocation.get("bank_account")) not in _CASH_SUBTYPES:
            continue
        changed_purpose = (len({account for account, _ in weights}) > 1
                           and any(subtypes.get(account) == "fixed_asset" for account, _ in weights)
                           and any(row.get("item_id") == item["id"] and row.get("kind") in {"credit", "writeoff", "reversal", "application"}
                                   and row.get("date", "9999-12-31") <= allocation["date"] for row in allocations))
        if changed_purpose:
            # Credits, writeoffs and offsets can change which original lines
            # remain payable. Never apply the original ratio to that new debt.
            moved = _dec(amount)
            offsets[item["account"]] += moved
            flows.append({"group": "needs_classification", "account": "adjusted-capital-settlement:" + allocation["id"],
                          "amount": -moved, "reason": "Payment of an adjusted mixed capital bill needs a reviewed purpose split"})
            issues.append({"code": "cash_flow_adjusted_bill_allocation", "item_id": item["id"],
                           "message": "A payment of an adjusted mixed capital bill needs a reviewed split between capital and operating costs."})
            continue
        for (account, _), was, now in zip(weights, before, after):
            if subtypes.get(account) != "fixed_asset" or now == was:
                continue
            moved = _dec(now - was)
            offsets[item["account"]] += moved
            flows.append({"group": "investing", "account": "capital-settlement:" + allocation["id"] + ":" + account,
                          "amount": -moved, "reason": "Payment for " + account.replace(":", _SEP)})
    return dict(offsets), flows, events, issues


def cash_flow(entity_path: Path | str, from_date: date, to_date: date) -> CashflowResult:
    """Compute the statement of cash flows for [from_date, to_date] inclusive.

    Opening balances are a starting point, never a cash flow:

    * A period that starts inside an imported fiscal summary is refused (the P&L
      accepts it, a cash flow cannot).
    * An opening entry dated on ``from_date`` is part of the beginning balances.
    * An opening entry dated after ``from_date`` and on or before ``to_date`` makes
      the beginning balances unknowable, so the report is refused.
    """
    from ..accrual.reports import CoverageError

    entity_path = Path(entity_path)
    conn = open_cache(entity_path)
    try:
        pnl = _compute_pnl(conn, from_date, to_date, coverage_view=journal_coverage_view())  # recorded income
    finally:
        conn.close()
    if pnl.meta.get("summary_included"):
        cutover = (pnl.meta.get("coverage") or {}).get("cutover_date")
        raise CoverageError(
            "A cash flow statement cannot start inside the imported fiscal summary, because the opening "
            "balances are its starting point. Start on or after the cutover date.", cutover, "from")
    conn = open_cache(entity_path)
    try:
        openings = opening_entries(conn, from_date, to_date)
        later = [o for o in openings if o["date"] > from_date]
        if later:
            first = min(o["date"] for o in later).isoformat()
            raise CoverageError(
                f"The opening balances are dated {first}, inside the period. Cash flows before opening balances "
                f"are unknown. Start the cash flow statement on {first}.", first, "from")
        begin = _account_balances_as_of(conn, from_date - timedelta(days=1))
        for account, amount in _opening_balances(conn, [o["id"] for o in openings]).items():
            begin[account] = begin.get(account, ZERO) + amount
        end = _account_balances_as_of(conn, to_date)
        subtypes = classify_accounts(conn, set(begin) | set(end))
        # A second, independent read: sum the bank and cash accounts straight from the postings.
        independent = sum((get_account_balance(conn, a, to_date) for a, t in subtypes.items() if t in _CASH_SUBTYPES), ZERO)
        offsets, flows, noncash, issues = _journal_adjustments(conn, from_date, to_date, subtypes, {o["id"] for o in openings})
    finally:
        conn.close()
    meta = {**pnl.meta, "noncash_events": noncash, "unresolved_issues": [*pnl.meta.get("unresolved_issues", []), *issues]}
    return build_cash_flow(from_date, to_date, pnl.totals["net_income"], begin, end, subtypes, meta,
                           independent_ending_cash=independent, balance_adjustments=offsets, activity_adjustments=flows)
