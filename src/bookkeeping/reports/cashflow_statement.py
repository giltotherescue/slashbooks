"""Statement of cash flows, indirect method.

This is the financial statement.  It is not the cash forecast in
``bookkeeping.cashflow``.

The engine starts from the period net income of the profit and loss statement and
adds the effect on cash of every change in a balance sheet account.  All math uses
``Decimal``.  The sign rule is one rule for every account: a ledger balance is
debit-positive, so the cash effect of a change in any non-cash account is
``-(ending balance - beginning balance)``.  An asset that grows uses cash.  A
liability that grows supplies cash.

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

Reconciliation
--------------
beginning cash + net change in cash = ending cash must equal the bank and cash
accounts on the balance sheet at the end date, to the cent.  When it does not, the
result carries ``reconciliation.ties = false`` and the amount of the difference,
and the command exits with code 1.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from .cache import open_cache
from .statements import _SEP, _account_balances_as_of, profit_and_loss
from .structure import SCHEMA_VERSION

ZERO = Decimal("0.00")

_CASH_SUBTYPES = {"bank", "cash"}
_OPERATING_SUBTYPES = ("receivable", "prepaid", "payable", "accrued_liabilities", "deferred_revenue",
                       "customer_deposits", "credit_card")
_FINANCING_NAME = re.compile(r"loan|note|mortgage|debt|line[-_ ]?of[-_ ]?credit|\bloc\b", re.IGNORECASE)
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
    return {name: _subtype(name, usage) for name in names}


def classify(account: str, subtype: str) -> tuple[str, str]:
    """Return ``(group, reason)`` for one balance sheet account."""
    if subtype in _CASH_SUBTYPES:
        return "cash", f"{subtype} account"
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
    if subtype == "other_liability" and _FINANCING_NAME.search(account.split(":", 1)[-1]):
        return "financing", "debt account"
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
        out.append("Method: indirect")
        for issue in self.meta.get("unresolved_issues", []):
            message = issue.get("message") or issue.get("code", "Unresolved issue").replace("_", " ")
            out.append(f"  - {message}")
        for line in self.lines:
            if line["type"] == "section":
                out += ["", line["label"], "-" * 40]
            elif line["type"] == "total":
                out.append(f"{line['label']:<42} {Decimal(line['amount']):>14,.2f}")
            elif "amount" in line:
                pad = "  " * line.get("indent", 1) if line["type"] in {"account", "computed"} else "  "
                out.append(f"{pad}{line['label']:<40} {Decimal(line['amount']):>14,.2f}")
        rec = self.reconciliation
        out += ["", "Reconciliation", "-" * 40,
                f"  Beginning cash {rec['beginning_cash']} + net change {rec['net_change_in_cash']} = {rec['ending_cash_per_statement']}",
                f"  Cash on the balance sheet at {self.to_date}: {rec['balance_sheet_cash']}"]
        if rec["ties"]:
            out.append("  The statement ties to the balance sheet cash accounts.")
        else:
            out.append(f"  DIFFERENCE: {rec['difference']}. The statement does not tie to the balance sheet cash accounts.")
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
) -> CashflowResult:
    """Pure builder: balances in, statement out.  No I/O."""
    groups: dict[str, list[tuple[str, Decimal, str, str]]] = {k: [] for k in GROUP_LABELS}
    cash_accounts: list[dict] = []
    beginning_cash = ending_cash = ZERO
    for account in sorted(set(begin) | set(end)):
        if not _is_balance_sheet(account):
            if account.split(":")[0] not in {"Income", "Expenses"}:
                groups["needs_classification"].append(
                    (account, -(end.get(account, ZERO) - begin.get(account, ZERO)), "unknown",
                     "account type is not an asset, liability, equity, income or expense"))
            continue
        before, after = begin.get(account, ZERO), end.get(account, ZERO)
        group, reason = classify(account, subtypes.get(account, "other_asset"))
        if group == "cash":
            beginning_cash += before
            ending_cash += after
            cash_accounts.append({"account": account, "beginning": _money(before), "ending": _money(after)})
            continue
        effect = -(after - before)
        if effect:
            groups[group].append((account, effect, subtypes.get(account, ""), reason))

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
            if subtype == "accumulated_depreciation":
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
    difference = ending_per_statement - ending_cash
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
        "difference": _money(difference),
        "ties": difference == 0,
        "cash_accounts": cash_accounts,
    }
    return CashflowResult(from_date, to_date, lines, totals, reconciliation, needs, dict(meta or {}))


_OPENING_SOURCES = {"quickbooks-opening", "accrual-opening"}


def opening_entries(conn: Any, from_date: date, to_date: date) -> list[dict]:
    """Imported opening-balance entries dated in [from_date, to_date].

    An entry is an opening entry when its ``import-source`` is ``quickbooks-opening``
    or ``accrual-opening``, or its ``accrual-opening`` metadata is ``true``.  A ledger
    cache without entry metadata is matched by the QuickBooks opening ``source_id``.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
    meta_column = "metadata_json" if "metadata_json" in columns else "'[]'"
    found = []
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
    pnl = profit_and_loss(entity_path, from_date, to_date)  # validates dates and coverage
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
    finally:
        conn.close()
    meta = dict(pnl.meta)
    return build_cash_flow(from_date, to_date, pnl.totals["net_income"], begin, end, subtypes, meta)
