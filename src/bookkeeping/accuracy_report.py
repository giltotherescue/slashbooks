"""Reproducible accuracy report for ``books backtest run --truth``.

The report joins three measurements and keeps them together:

- category agreement against a ground-truth file (``compare_categories``),
- coverage: how many truth rows were auto-posted, waiting, or missing,
- P&L line variance and transaction matching against the QuickBooks export.

It never states a percentage without the row count and the coverage split
beside it. It also lists what the run does not measure, so a reader does not
take the number for more than it is.

The JSON schema is versioned by ``schema_version``. Add fields freely; change
or remove a field only with a new ``schema_version``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .compare import (
    ComparisonReport,
    _materiality_thresholds,
    compare_categories,
)
from .entity import Entity

SCHEMA_VERSION = 1
REPORT_JSON = "accuracy-report.json"
REPORT_MD = "accuracy-report.md"

# Below this many compared rows, a rate is flagged as indicative only.
SMALL_SAMPLE_N = 30

DOES_NOT_MEASURE = [
    "Rows outside the truth file or outside the period. Only the truth rows are scored.",
    "Whether the truth file is correct. It is taken as supplied and is not audited.",
    "Pending rows. A row that waits for review has no category yet, so it is counted in "
    "coverage and is not scored as right or wrong.",
    "Agent judgment in the review queue. In a backtest only the rules in entity.json "
    "(category_rules) post automatically.",
    "Leakage. The report cannot tell whether the rules were written after reading the "
    "truth file. No train and test split is enforced.",
    "Dollar-weighted accuracy. Agreement counts transactions, not amounts.",
    "Split entries and transfers between bank accounts. They have no single category, "
    "so they are counted as not comparable and left out of the agreement rate.",
    "Tax treatment, accrual timing and the balance sheet beyond the totals compared.",
    "Transaction matching to QuickBooks. It uses date, amount and description, not a "
    "shared ID, so matched_count is a heuristic count.",
]


def _git_commit() -> tuple[Optional[str], Optional[bool]]:
    """Return (commit, dirty) when this engine runs from a git checkout, else Nones."""
    here = Path(__file__).resolve()
    try:
        top = subprocess.run(
            ["git", "-C", str(here.parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        # An installed copy inside some other repository must not claim its commit.
        if Path(top).resolve() != here.parents[2]:
            return None, None
        commit = subprocess.run(
            ["git", "-C", top, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", top, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        return commit or None, bool(status)
    except (OSError, subprocess.SubprocessError):
        return None, None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pnl_variance(comparison: Optional[ComparisonReport], abs_t: Decimal, pct_t: Decimal) -> dict:
    lines: list[dict] = []
    if comparison is not None:
        for row in comparison.matched_rows:
            if row["grain"] == "pnl":
                ours, ref = Decimal(row["ours"]), Decimal(row["reference"])
                lines.append({
                    "account": row["name"], "ours": str(ours), "reference": str(ref),
                    "delta": str(ours - ref), "material": False,
                })
        material_keys = {d.key for d in comparison.material_diffs}
        for diff in comparison.material_diffs + comparison.immaterial_diffs:
            if diff.grain == "pnl":
                lines.append({
                    "account": diff.account, "ours": str(diff.ours),
                    "reference": str(diff.reference), "delta": str(diff.delta),
                    "material": diff.key in material_keys,
                })
    lines.sort(key=lambda line: line["account"])
    return {
        "lines_compared": len(lines),
        "lines_matched": sum(1 for line in lines if Decimal(line["delta"]) == 0),
        "lines_material": sum(1 for line in lines if line["material"]),
        "lines": lines,
    }


def headline(agreement: dict, coverage: dict) -> str:
    """One sentence with the rate, n and the coverage split together."""
    rate = agreement["rate"]
    rate_text = "n/a" if rate is None else f"{rate * 100:.1f}%"
    return (
        f"Category agreement {rate_text} on {agreement['compared']} compared rows "
        f"({agreement['agree']} agree, {agreement['disagree']} differ). "
        f"Of {coverage['truth_rows']} truth rows: {coverage['compared']} compared, "
        f"{coverage['pending']} pending review, {coverage['missing']} missing, "
        f"{coverage['not_comparable']} not comparable."
    )


def build_accuracy_report(
    entity: Entity,
    truth_csv: Path,
    comparison: Optional[ComparisonReport],
    from_date: date,
    to_date: date,
    *,
    seed: Optional[int] = None,
    generated_at: Optional[str] = None,
) -> dict:
    """Build the accuracy report dict. Reads the ledger; writes nothing."""
    categories = compare_categories(entity, truth_csv, from_date, to_date)
    abs_t, pct_t = _materiality_thresholds(entity)
    commit, dirty = _git_commit()
    agreement = categories["agreement"]
    coverage = categories["coverage"]
    compared = agreement["compared"]

    disagreements = [
        row for row in categories["per_transaction"] if row["status"] == "disagree"
    ]
    notes: list[str] = []
    if compared < SMALL_SAMPLE_N:
        notes.append(
            f"Only {compared} rows were compared. Fewer than {SMALL_SAMPLE_N} is too few "
            "to read the rate as more than indicative."
        )
    if coverage["truth_rows"] and coverage["compared"] < coverage["truth_rows"]:
        notes.append(
            "Not every truth row was compared. Read the rate together with coverage."
        )

    match_summary = {
        "matched_count": comparison.matched_count if comparison else 0,
        "unmatched_ours": len(comparison.unmatched_ours) if comparison else 0,
        "unmatched_qb": len(comparison.unmatched_qb) if comparison else 0,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "report": "slashbooks-accuracy-report",
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine": {"version": __version__, "git_commit": commit, "git_dirty": dirty},
        "seed": seed,
        "period": {"from": str(from_date), "to": str(to_date)},
        "truth": {
            "file": Path(truth_csv).name,
            "sha256": _sha256_file(Path(truth_csv)),
            "rows": categories["truth_rows"],
        },
        "thresholds": {
            "material_abs": str(abs_t),
            "material_pct": str(pct_t),
            "small_sample_n": SMALL_SAMPLE_N,
        },
        "n": categories["truth_rows"],
        "headline": headline(agreement, coverage),
        "category_agreement": {
            "compared": agreement["compared"],
            "agree": agreement["agree"],
            "disagree": agreement["disagree"],
            "rate": agreement["rate"],
            "end_to_end_rate": agreement["end_to_end_rate"],
            "per_account": categories["per_account"],
            "confusion": categories["confusion"],
            "disagreements": disagreements,
        },
        "coverage": coverage,
        "pnl_line_variance": _pnl_variance(comparison, abs_t, pct_t),
        "matched_count": match_summary["matched_count"],
        "transaction_matching": match_summary,
        "comparison_errors": list(comparison.errors) if comparison else ["Comparison did not run."],
        "notes": notes,
        "does_not_measure": list(DOES_NOT_MEASURE),
        "per_transaction": categories["per_transaction"],
    }


def _pct(rate: Optional[float], n: int) -> str:
    """A percentage is always written with its n."""
    return f"{'n/a' if rate is None else f'{rate * 100:.1f}%'} (n={n})"


def render_accuracy_markdown(report: dict) -> str:
    """Render the report as Markdown. Every percentage carries its n."""
    ca = report["category_agreement"]
    cov = report["coverage"]
    lines = [
        "# Slashbooks accuracy report",
        "",
        f"**Period:** {report['period']['from']} through {report['period']['to']}",
        f"**Engine:** {report['engine']['version']}"
        + (f", commit {report['engine']['git_commit']}" if report["engine"]["git_commit"] else "")
        + (" (uncommitted changes)" if report["engine"].get("git_dirty") else ""),
        f"**Seed:** {report['seed'] if report['seed'] is not None else 'none'}",
        f"**Truth file:** {report['truth']['file']}, sha256 {report['truth']['sha256']}",
        f"**Generated:** {report['generated_at']}",
        "",
        "## Result",
        "",
        report["headline"],
        "",
        "## Coverage",
        "",
        "| Truth rows | Compared | Pending review | Missing | Not comparable |",
        "|---|---|---|---|---|",
        f"| {cov['truth_rows']} | {cov['compared']} | {cov['pending']} | "
        f"{cov['missing']} | {cov['not_comparable']} |",
        "",
        f"Compared share of truth rows: {_pct(cov['compared_rate'], cov['truth_rows'])}.",
        f"Agreement among compared rows: {_pct(ca['rate'], ca['compared'])}.",
        f"Agreement counting every truth row (pending and missing as not right): "
        f"{_pct(ca['end_to_end_rate'], cov['truth_rows'])}.",
        "",
    ]
    for note in report["notes"]:
        lines.append(f"> {note}")
    if report["notes"]:
        lines.append("")

    lines += ["## Agreement by correct account", ""]
    if ca["per_account"]:
        lines += [
            "| Correct account | Truth rows | Compared | Agree | Agreement | Pending | Missing |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in ca["per_account"]:
            lines.append(
                f"| {row['account']} | {row['truth_rows']} | {row['compared']} | {row['agree']} | "
                f"{_pct(row['agreement_rate'], row['compared'])} | {row['pending']} | {row['missing']} |"
            )
    else:
        lines.append("No truth rows.")
    lines.append("")

    wrong = [c for c in ca["confusion"] if not c["agree"]]
    lines += ["## Where categories differ", ""]
    if wrong:
        lines += ["| Correct account | Our account | Rows |", "|---|---|---|"]
        lines += [f"| {c['truth']} | {c['ours']} | {c['count']} |" for c in wrong]
    else:
        lines.append("No differing categories among compared rows.")
    lines.append("")

    pv = report["pnl_line_variance"]
    lines += [
        "## Profit and loss lines against QuickBooks",
        "",
        f"{pv['lines_matched']} of {pv['lines_compared']} lines match; "
        f"{pv['lines_material']} differ by a material amount "
        f"(at least {report['thresholds']['material_abs']} or "
        f"{report['thresholds']['material_pct']} of the line).",
        "",
    ]
    if pv["lines"]:
        lines += ["| Line | Ours | QuickBooks | Difference | Material |", "|---|---|---|---|---|"]
        lines += [
            f"| {ln['account']} | {ln['ours']} | {ln['reference']} | {ln['delta']} | "
            f"{'yes' if ln['material'] else 'no'} |"
            for ln in pv["lines"]
        ]
        lines.append("")
    tm = report["transaction_matching"]
    lines += [
        "## Transaction matching",
        "",
        f"- Matched: {tm['matched_count']}",
        f"- In our books only: {tm['unmatched_ours']}",
        f"- In QuickBooks only: {tm['unmatched_qb']}",
        "",
    ]
    if report["comparison_errors"]:
        lines += ["## Comparison errors", ""]
        lines += [f"- {error}" for error in report["comparison_errors"]]
        lines.append("")

    lines += ["## What this does not measure", ""]
    lines += [f"- {item}" for item in report["does_not_measure"]]
    lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


def write_accuracy_report(report: dict, out_dir: Path) -> tuple[Path, Path]:
    """Write accuracy-report.json and accuracy-report.md into *out_dir*."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / REPORT_JSON
    md_path = out_dir / REPORT_MD
    _atomic_write(json_path, json.dumps(report, indent=2, sort_keys=True) + "\n")
    _atomic_write(md_path, render_accuracy_markdown(report))
    return json_path, md_path
