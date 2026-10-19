"""Capture every user-visible output of a synthetic company into named golden texts."""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from golden_support import READINESS_AS_OF, Capture, Normalizer, frozen_clock, pretty_json, run_cli

ACCRUAL_KINDS = ("aging", "schedules", "adjustments", "support", "reconciliation", "outstanding")

PLANS = {
    "cash": {
        "ranges": [("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28"), ("2026-03-01", "2026-03-31"),
                   ("2026-01-01", "2026-03-31")],
        "as_of": ["2026-01-31", "2026-02-28", "2026-03-31"],
        "readiness": ["2026-01", "2026-02", "2026-03"],
        "exports": [("2026-01-01", "2026-01-31"), ("2026-01-01", "2026-03-31")],
        "queue_show": ["chk-0105-rent", "crd-0118-travel", "chk-0330-deposit"],
        "tax_form": "schc",
        "tax_range": ("2026-01-01", "2026-03-31"),
        "quarter": ("Q1", "2026"),
        "ask": ["How much did we earn in January?", "What were our total expenses this year?",
                "How much did we spend on software in March?", "How much did we pay Maple Street last quarter?",
                "How are we doing this year?", "What do we owe?", "What is the balance sheet?",
                "What were owner draws this month?",
                "How much cash did we spend on software in March?",
                "What was our accrual net income in March?"],
        "accrual_as_of": [],
    },
    "accrual": {
        "ranges": [("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28"), ("2026-03-01", "2026-03-31"),
                   ("2026-01-01", "2026-03-31")],
        "as_of": ["2025-12-31", "2026-01-31", "2026-02-28", "2026-03-31"],
        "readiness": ["2026-01", "2026-02", "2026-03"],
        "exports": [("2026-01-01", "2026-01-31"), ("2026-01-01", "2026-03-31")],
        "queue_show": [],
        "tax_form": "1120s",
        "tax_range": ("2026-01-01", "2026-03-31"),
        "quarter": ("Q1", "2026"),
        "ask": ["How much did we earn in January?", "What were our total expenses this year?",
                "How much did we spend on software in March?", "How are we doing this year?",
                "What do we owe?", "Who owes us money?", "Show receivables aging", "How much is prepaid?",
                "What is our deferred revenue?", "What is the balance sheet?",
                "How much cash did we spend on software in March?",
                "What was our cash net income in March?"],
        "accrual_as_of": ["2026-01-31", "2026-03-31"],
        "accrual_ranges": [("2026-01-01", "2026-03-31"), ("2026-03-01", "2026-03-31")],
    },
    "quickbooks": {
        "ranges": [("2026-07-01", "2026-07-31"), ("2026-01-01", "2026-07-31"), ("2026-06-01", "2026-06-30")],
        "as_of": ["2026-06-30", "2026-07-31"],
        "readiness": ["2026-06", "2026-07"],
        "exports": [("2026-07-01", "2026-07-31")],
        "queue_show": ["customer-receipt", "payroll-payment"],
        "tax_form": "1065",
        "tax_range": ("2026-01-01", "2026-07-31"),
        "quarter": ("Q3", "2026"),
        "ask": ["How much did we earn in July?", "What were our total expenses this year?", "How are we doing this year?",
                "What do we owe?", "Who owes us money?", "Show receivables aging", "How much is prepaid?",
                "What is our deferred revenue?", "What is the balance sheet?",
                "How much cash did we spend on operating costs in July?"],
        "accrual_as_of": ["2026-06-30", "2026-07-31"],
        "accrual_ranges": [("2026-07-01", "2026-07-31"), ("2026-01-01", "2026-07-31")],
    },
}

# Commands whose remote/hosted contract (`command_spec`) is part of the stable surface.
SPEC_COMMANDS = [
    ["report", "pnl", "--entity", "E", "--from", "2026-01-01", "--to", "2026-01-31"],
    ["report", "balance-sheet", "--entity", "E", "--as-of", "2026-01-31", "--output", "out.txt"],
    ["report", "trial-balance", "--entity", "E", "--as-of", "2026-01-31"],
    ["report", "general-ledger", "--entity", "E", "--from", "2026-01-01", "--to", "2026-01-31", "--format", "json"],
    ["ask", "--entity", "E", "How much did we earn in January?"],
    ["readiness", "--entity", "E", "--period", "2026-01", "--as-of", "2026-02-02"],
    ["export", "--entity", "E", "--from", "2026-01-01", "--to", "2026-01-31", "--output-dir", "out"],
    ["queue", "list", "--entity", "E"],
    ["queue", "summary", "--entity", "E"],
    ["queue", "propose", "--entity", "E", "--source-id", "x", "--category", "Expenses:Fees", "--reasoning", "r"],
    ["queue", "confirm", "--entity", "E", "--item", "x"],
    ["reconcile", "--entity", "E", "--account", "Assets:Bank:Checking", "--source-balance", "1.00", "--as-of", "2026-01-31"],
    ["coverage", "check", "--entity", "E", "--from", "2026-01-01", "--to", "2026-01-31", "--output", "cov.txt"],
    ["tax", "map", "--entity", "E", "--year", "2026"],
    ["tax", "export", "--entity", "E", "--year", "2026", "--output-dir", "out"],
    ["work", "--entity", "E", "--from", "2026-01-01", "--to", "2026-01-31"],
    ["balance", "check", "--entity", "E", "--input", "in.json"],
    ["compare", "period", "--entity", "E", "--qb-folder", "qb", "--from", "2026-01-01", "--to", "2026-01-31"],
    ["accrual", "report", "--entity", "E", "--kind", "aging", "--as-of", "2026-01-31"],
    ["accrual", "item", "preview", "--entity", "E", "--input", "in.json"],
    ["accrual", "item", "settle-apply", "--entity", "E", "--input", "in.json"],
    ["accrual", "schedule", "due-preview", "--entity", "E", "--through", "2026-01-31", "--operation-id", "op"],
    ["accrual", "journal", "due-reversals", "--entity", "E", "--as-of", "2026-02-01"],
    ["accrual", "journal", "due-reversals", "--entity", "E", "--as-of", "2026-02-01", "--apply"],
    ["accrual", "period", "apply", "--entity", "E", "--input", "in.json"],
    ["accrual", "rules", "set-apply", "--entity", "E", "--input", "in.json"],
    ["accrual", "rules", "list", "--entity", "E"],
    ["accrual", "opening", "preview", "--entity", "E", "--input", "in.json"],
    ["accrual", "migration", "apply", "--entity", "E", "--input", "in.json"],
    ["entity", "init", "dir", "--basis", "accrual"],
    ["entity", "account-add", "dir", "--account", "Expenses:Fees"],
    ["ledger", "snapshot", "--entity", "E"],
]


def capture_shared(capture: Capture) -> None:
    """Company-independent contracts: command specs, schemas, procedures."""
    from bookkeeping.remote_contract import command_spec

    specs: dict[str, Any] = {}
    for argv in SPEC_COMMANDS:
        try:
            spec = command_spec(list(argv))
            specs[" ".join(argv)] = {**spec, "path": list(spec["path"])}
        except Exception as exc:  # the contract may legitimately reject; record that too
            specs[" ".join(argv)] = {"error": f"{type(exc).__name__}: {exc}"}
    capture.data("remote-command-specs", specs)
    for name, argv in (("accrual-schema", ["accrual", "schema"]), ("accrual-capabilities", ["accrual", "capabilities"]),
                       ("procedures-list", ["procedures", "list"]), ("tax-lines-schc", ["tax", "lines", "--form", "schc", "--format", "json"]),
                       ("tax-lines-1120s", ["tax", "lines", "--form", "1120s"])):
        capture.cli(name, argv)


def _slug(*parts: str) -> str:
    return "_".join(p.replace("-", "") if len(p) == 10 and p[4] == "-" else p for p in parts)


def capture_company(kind: str, root: Path, work: Path) -> Capture:
    plan = PLANS[kind]
    root = root.resolve()
    capture = Capture(Normalizer(root, work.resolve()))
    entity_arg = ["--entity", str(root)]
    accrual = kind in {"accrual", "quickbooks"}

    # Tax setup is a person's choice in entity.json; make it part of the company before reading it.
    run_cli(["tax", "set", *entity_arg, "--return-form", plan["tax_form"]])

    # Statements: every range and as-of date, text and JSON.
    for start, end in plan["ranges"]:
        for report in ("pnl", "general-ledger"):
            for fmt in ("text", "json"):
                capture.cli(f"reports/{report}_{start}_{end}_{fmt}", ["report", report, *entity_arg, "--from", start, "--to", end, "--format", fmt])
    for as_of in plan["as_of"]:
        for report in ("balance-sheet", "trial-balance"):
            for fmt in ("text", "json"):
                capture.cli(f"reports/{report}_{as_of}_{fmt}", ["report", report, *entity_arg, "--as-of", as_of, "--format", fmt])
    for fmt in ("text", "json"):
        target = work / f"pnl-{kind}-{fmt}.out"
        capture.cli(f"reports/pnl_to_file_{fmt}", ["report", "pnl", *entity_arg, "--from", plan["ranges"][-1][0],
                                                   "--to", plan["ranges"][-1][1], "--format", fmt, "--output", str(target)])
        if target.exists():
            capture.put(f"reports/pnl_file_contents_{fmt}.{'json' if fmt == 'json' else 'txt'}", target.read_text(encoding="utf-8"))
    capture.cli("reports/quarterly-review", ["quarterly-review", *entity_arg, "--quarter", plan["quarter"][0], "--year", plan["quarter"][1]])

    # Accrual reports.
    if accrual:
        for report_kind in ACCRUAL_KINDS:
            for as_of in plan["accrual_as_of"]:
                for fmt in ("json", "text"):
                    capture.cli(f"accrual/{report_kind}_asof_{as_of}_{fmt}",
                                ["accrual", "report", *entity_arg, "--kind", report_kind, "--as-of", as_of, "--format", fmt])
        for start, end in plan["accrual_ranges"]:
            for report_kind in ("adjustments", "schedules", "support"):
                for fmt in ("json", "text"):
                    capture.cli(f"accrual/{report_kind}_range_{start}_{end}_{fmt}",
                                ["accrual", "report", *entity_arg, "--kind", report_kind, "--from", start, "--to", end, "--format", fmt])
        for fmt in ("json", "text"):
            capture.cli(f"accrual/accounts_{fmt}", ["accrual", "accounts", *entity_arg, "--format", fmt])
        capture.cli("accrual/rules_list_json", ["accrual", "rules", "list", *entity_arg])
        capture.cli("accrual/rules_list_text", ["accrual", "rules", "list", *entity_arg, "--format", "text"])
        capture.cli("accrual/due_reversals_preview", ["accrual", "journal", "due-reversals", *entity_arg, "--as-of", "2026-12-31"])
        capture.cli("accrual/schedule_list", ["accrual", "schedule", "list", *entity_arg])
        capture.cli("accrual/item_list", ["accrual", "item", "list", *entity_arg])
        if kind == "accrual":
            facts = work / "rule-facts.json"
            facts.write_text(json.dumps({"counterparty": "Granite Hosting", "amount": "-1200.00", "account": "Expenses:Software",
                                         "description": "GRANITE HOSTING 5001", "date": "2026-04-01", "service_start": "2026-04-01",
                                         "source_id": "bank-granite-0401", "funding_account": "Assets:Bank:Checking"}))
            capture.cli("accrual/rules_match", ["accrual", "rules", "match", *entity_arg, "--input", str(facts)])
            capture.cli("accrual/receipt_close_2026-01", ["accrual", "receipt", *entity_arg, "--id", "close-2026-01"])

    # Readiness for every month.
    for period in plan["readiness"]:
        capture.cli(f"readiness/{period}", ["readiness", *entity_arg, "--period", period, "--as-of", READINESS_AS_OF])

    # Export package: file list and contents. A company with open review work is refused
    # until a person passes --override; capture the refusal and the overridden package.
    for start, end in plan["exports"]:
        out = work / f"export-{kind}-{start}-{end}"
        capture.cli(f"export/{start}_{end}_command", ["export", *entity_arg, "--from", start, "--to", end, "--output-dir", str(out)])
        if not out.exists() and kind == "cash":
            capture.cli(f"export/{start}_{end}_override_command",
                        ["export", *entity_arg, "--from", start, "--to", end, "--output-dir", str(out), "--override"])
        if out.exists():
            capture.tree(f"export/{start}_{end}", out)
    capture.cli("export/sanity_check", ["sanity-check", *entity_arg, "--from", plan["ranges"][-1][0], "--to", plan["ranges"][-1][1]])

    # Tax map and exports.
    start, end = plan["tax_range"]
    for fmt in ("text", "json"):
        capture.cli(f"tax/map_{fmt}", ["tax", "map", *entity_arg, "--from", start, "--to", end, "--format", fmt])
    tax_out = work / f"tax-{kind}"
    capture.cli("tax/export_command", ["tax", "export", *entity_arg, "--from", start, "--to", end, "--output-dir", str(tax_out)])
    if tax_out.exists():
        capture.tree("tax/export", tax_out)

    # Questions.
    for index, question in enumerate(plan["ask"], 1):
        capture.cli(f"ask/q{index:02d}", ["ask", *entity_arg, question, "--today", READINESS_AS_OF])
        capture.manifest[f"ask/q{index:02d}.txt"] = {**capture.manifest.get(f"ask/q{index:02d}.txt", {}), "question": question}

    # Queue, coverage, work, compare-free summaries.
    capture.cli("queue/list", ["queue", "list", *entity_arg])
    capture.cli("queue/list_open", ["queue", "list", *entity_arg, "--status", "open"])
    capture.cli("queue/summary", ["queue", "summary", *entity_arg])
    capture.cli("queue/taught", ["queue", "taught", *entity_arg])
    capture.cli("queue/duplicates", ["queue", "duplicates", *entity_arg, "--json"])
    capture.cli("queue/transfer_candidates", ["queue", "transfer-candidates", *entity_arg])
    capture.cli("queue/transfer_exceptions", ["queue", "transfer-exceptions", *entity_arg])
    transfers = sorted(path.stem for path in (root / "review-queue").glob("transfer-pair-*.json"))
    for item_id in [*plan["queue_show"], *transfers]:
        capture.cli(f"queue/show_{item_id}", ["queue", "show", *entity_arg, "--item", item_id])
    capture.cli("queue/split_templates", ["queue", "split-template-list", *entity_arg])
    for fmt in ("text", "json"):
        capture.cli(f"coverage/check_{fmt}", ["coverage", "check", *entity_arg, "--from", plan["ranges"][-1][0],
                                              "--to", plan["ranges"][-1][1], "--format", fmt])
        capture.cli(f"work/summary_{fmt}", ["work", *entity_arg, "--format", fmt])
    capture.cli("coverage/statement_list", ["coverage", "statement-list", *entity_arg])
    capture.cli("entity/related_entities", ["entity", "related-entity", "list", str(root)])

    # Python surfaces the host calls directly: routine plan and context, integrity, ledger state.
    _capture_library(capture, kind, root, accrual)
    _capture_state(capture, kind, root, work)
    return capture


def _capture_library(capture: Capture, kind: str, root: Path, accrual: bool) -> None:
    with frozen_clock():
        from bookkeeping.entity import load_entity
        from bookkeeping.ledger.importer import check_integrity, check_store_integrity
        from bookkeeping.ledger.store import LedgerStore
        from bookkeeping.routine import routine_context, routine_plan

        entity = load_entity(root)
        open_accounts = {o.account for o in LedgerStore(root / "ledger.sqlite").load_opens()}
        categories = [c for c in ("Expenses:Fees", "Expenses:Rent", "Income:Consulting", "Expenses:Operating-Costs")
                      if c in open_accounts]
        rules = [{"id": "golden-rule-fees", "source_account_id": "checking", "counterparty_key": "ACH DEBIT REF",
                  "category": "Expenses:Fees", "approved_ledger_account": "Assets:Bank:Checking", "max_abs_amount": "300.00"}]
        policy = {"version": 1, "enabled": True, "rules": rules if kind == "cash" else [],
                  "review_categories": categories, "answer_roles": ["client"]}
        capture.data("routine/context", routine_context(entity))
        capture.data("routine/plan_all", routine_plan(entity, policy))
        capture.data("routine/plan_closed_through", routine_plan(entity, policy, closed_through="2026-02-28"))
        integrity = check_integrity(entity)
        capture.data("state/integrity", {"status": integrity.status, "message": getattr(integrity, "message", "")})
        store = LedgerStore(root / "ledger.sqlite")
        capture.data("state/store_integrity", {"status": check_store_integrity(store).status})


def _capture_state(capture: Capture, kind: str, root: Path, work: Path) -> None:
    """Ledger digest and meta, entity.json bytes, accrual records, audit events, company files, bundle manifest."""
    with frozen_clock():
        from bookkeeping import migration_bundle
        from bookkeeping.ledger.store import LedgerStore

        store = LedgerStore(root / "ledger.sqlite")
        counts = store.counts()
        capture.data("state/ledger", {"content_digest": store.content_digest(), "counts": _jsonable(counts)})
        with contextlib.closing(sqlite3.connect(f"file:{root / 'ledger.sqlite'}?mode=ro", uri=True)) as conn:
            meta = {k: v for k, v in conn.execute("SELECT key, value FROM meta ORDER BY key")}
            tables = {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                      for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}
        capture.data("state/ledger_meta", meta)
        capture.data("state/ledger_table_row_counts", tables)
        capture.data("state/audit_events", [_jsonable(event) for event in store.load_audit_events()])
        capture.data("state/accounts", [_jsonable(o) for o in store.load_opens()])
        capture.data("state/entries", [_jsonable(e) for e in store.load_entries()])
        if kind != "cash":
            from bookkeeping.accrual.store import TABLES, list_records
            for table in TABLES:
                capture.data(f"state/accrual_{table}", list_records(store, table))

        # Company directory: every small text file, except the SQLite database, its backups and the lock.
        skipped = {".books.lock", "ledger.sqlite"}
        names = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
                       and str(p.relative_to(root)) not in skipped and not str(p.relative_to(root)).startswith("backups/")
                       and not p.name.endswith(("-wal", "-shm")))
        capture.put("files/FILES.txt", "\n".join(names) + f"\nbackups: {len(list((root / 'backups').glob('*')))} file(s)\n")
        for relative in names:
            path = root / relative
            text = path.read_text(encoding="utf-8", errors="replace")
            pretty = pretty_json(text) if path.suffix == ".json" else None
            capture.put(f"files/{relative}", pretty or text)

        # The migration bundle a host receives: manifest only (no SQLite file bytes).
        bundle = work / f"{kind}-bundle.zip"
        result = migration_bundle.build_bundle(root, bundle)
        # bundle_sha256 and manifest_sha256 are hashes of bytes that hold the temp path; the manifest below
        # carries the same facts with per-file hashes taken over the path-normalized content.
        result = {k: v for k, v in result.items() if k not in {"bundle_sha256", "manifest_sha256"}}
        capture.data("bundle/export_result", result)
        if bundle.exists():
            loaded = migration_bundle.load_bundle(bundle)
            manifest = json.loads(json.dumps(loaded["manifest"]))
            for entry in manifest.get("files", []):
                if entry.get("path") == "ledger.sqlite":
                    entry.pop("sha256", None)  # SQLite file bytes; its content is covered by content_digest
                    entry.pop("size", None)
                    continue
                normalized = capture.normalize(loaded["files"][entry["path"]].decode("utf-8")).encode("utf-8")
                entry["sha256"] = hashlib.sha256(normalized).hexdigest()
                entry["size"] = len(normalized)
            capture.data("bundle/manifest", manifest)


def _jsonable(value: Any) -> Any:
    from dataclasses import asdict, is_dataclass
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
