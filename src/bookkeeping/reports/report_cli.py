"""Extra ``books report`` flags and commands, kept out of ``statements.py``.

``statements.add_parser`` calls :func:`extend_parser`; ``statements._run`` calls
:func:`run_extended` first.  ``run_extended`` returns ``None`` for every call it
does not own, so the existing commands print exactly what they printed before.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Optional


def extend_parser(report_sub: Any, common_args: Any, pnl_p: Any, bs_p: Any) -> None:
    for parser in (pnl_p, bs_p):
        parser.add_argument(
            "--structure", action="store_true",
            help="With --format json, add schema_version 2 and a structure object with section, "
                 "subtotal and total rows (Gross Profit, Total Assets, Total Liabilities and Equity)",
        )

    cf_p = report_sub.add_parser("cashflow", help="Statement of cash flows (indirect method)")
    cf_p.add_argument("--from", dest="from_date", required=True, help="Start date (YYYY-MM-DD)")
    cf_p.add_argument("--to", dest="to_date", required=True, help="End date (YYYY-MM-DD)")
    common_args(cf_p)
    for parser in (pnl_p, bs_p, cf_p):
        parser.add_argument(
            "--compare", choices=["previous", "prior_year"], default=None,
            help="Add prior-period amounts, the change, and (P&L) each line as a percent of total revenue. "
                 "JSON output carries schema_version 2 and a comparison object",
        )


def _emit(content: str, output: Optional[Path]) -> None:
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        print(f"Written to {output}")
    else:
        print(content)


def run_extended(args: Any, entity: Path, output: Optional[Path], fmt: str) -> Optional[int]:
    rc = getattr(args, "report_command", None)
    structure = getattr(args, "structure", False)
    compare = getattr(args, "compare", None)
    if compare and rc in {"pnl", "balance-sheet", "cashflow"}:
        return _run_compare(args, entity, output, fmt, rc, compare)
    if rc == "pnl" and structure and fmt == "json":
        from .statements import profit_and_loss
        from .structure import structured_json
        result = profit_and_loss(entity, date.fromisoformat(args.from_date), date.fromisoformat(args.to_date))
        _emit(structured_json(result), output)
        return 0
    if rc == "balance-sheet" and structure and fmt == "json":
        from .statements import balance_sheet
        from .structure import structured_json
        _emit(structured_json(balance_sheet(entity, date.fromisoformat(args.as_of))), output)
        return 0
    if rc == "cashflow":
        return _run_cashflow(args, entity, output, fmt)
    return None


def _run_compare(args: Any, entity: Path, output: Optional[Path], fmt: str, rc: str, mode: str) -> int:
    import sys
    from ..accrual.reports import CoverageError
    from . import comparative

    try:
        if rc == "pnl":
            result = comparative.compare_pnl(entity, date.fromisoformat(args.from_date), date.fromisoformat(args.to_date), mode)
        elif rc == "balance-sheet":
            result = comparative.compare_balance_sheet(entity, date.fromisoformat(args.as_of), mode)
        else:
            result = comparative.compare_cashflow(entity, date.fromisoformat(args.from_date), date.fromisoformat(args.to_date), mode)
    except CoverageError as exc:
        first = f" The first available date is {exc.first_date}." if exc.first_date else ""
        print(f"Error: {exc}{first}", file=sys.stderr)
        return 1
    _emit(result.to_json() if fmt == "json" else result.to_text(), output)
    if not result.ties:
        print(f"Error: the cash flow statement does not tie to the balance sheet cash accounts. "
              f"Difference: {result.tie_difference}.", file=sys.stderr)
        return 1
    return 0


def _run_cashflow(args: Any, entity: Path, output: Optional[Path], fmt: str) -> int:
    import sys
    from ..accrual.reports import CoverageError
    from .cashflow_statement import cash_flow

    try:
        result = cash_flow(entity, date.fromisoformat(args.from_date), date.fromisoformat(args.to_date))
    except CoverageError as exc:
        lines = [f"Error: {exc}"]
        if exc.first_date:
            from_date, to_date = args.from_date, args.to_date
            if exc.field == "from":
                from_date, to_date = exc.first_date, max(to_date, exc.first_date)
            else:
                to_date, from_date = exc.first_date, min(from_date, exc.first_date)
            lines.append(f"The first available date is {exc.first_date}. Run: books report cashflow "
                         f"--entity {args.entity} --from {from_date} --to {to_date}")
        print("\n".join(lines), file=sys.stderr)
        return 1
    _emit(result.to_json() if fmt == "json" else result.to_text(), output)
    if not result.reconciliation["ties"]:
        print(f"Error: the cash flow statement does not tie to the balance sheet cash accounts. "
              f"Difference: {result.reconciliation['difference']}.", file=sys.stderr)
        return 1
    return 0
