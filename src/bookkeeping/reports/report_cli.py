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
    return None
