"""Seeded synthetic company for backtest accuracy tests.

Every value is invented. The generator emits, from one seed:

- ``banksync.json``       bank rows in the BankSync download shape
- ``truth.csv``           the correct category account for each row
- ``qb/``                 a QuickBooks-format export that agrees with the truth
- ``entity.json``         category rules that cover only some vendors, and one
                          deliberately wrong rule

It also returns a manifest with the expected counts, computed here and not by
the engine, so a test can check the engine against known answers.

Usage:
    python3 tests/fixtures/synthetic/generate.py --seed 1 --rows 120 --out DIR
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

COMPANY = "Synthetic Studio LLC"
BANK_ACCOUNT = "Assets:Bank:Checking"
PERIOD_FROM = date(2026, 1, 1)
PERIOD_TO = date(2026, 3, 31)

# (vendor, correct account, lowest cents, highest cents)
EXPENSE_VENDORS = [
    ("Cloudhost Pro", "Expenses:Software", 1500, 9000),
    ("Pixel Tools", "Expenses:Software", 900, 4500),
    ("Paper Barn", "Expenses:Office-Supplies", 800, 6000),
    ("Inkwell Supply", "Expenses:Office-Supplies", 1200, 7000),
    ("Corner Cafe", "Expenses:Meals", 700, 3500),
    ("Noodle House", "Expenses:Meals", 900, 4200),
    ("Skyline Air", "Expenses:Travel", 12000, 60000),
    ("Metro Rail", "Expenses:Travel", 500, 5000),
    ("Brightad", "Expenses:Advertising", 5000, 40000),
    ("Handbill Press", "Expenses:Advertising", 3000, 20000),
]
INCOME_VENDORS = [
    ("Maple Retail", "Income:Consulting-Revenue", 80000, 400000),
    ("Harbor Foods", "Income:Consulting-Revenue", 60000, 300000),
]
# Vendors with a category rule. The first EXPENSE_COVERED expense vendors in the
# list above, plus every income vendor, get a rule. Index 0 of the covered
# expense vendors gets a wrong account on purpose.
EXPENSE_COVERED = 6
WRONG_RULE_VENDOR = "Pixel Tools"
WRONG_RULE_ACCOUNT = "Expenses:Office-Supplies"


def _cents(value: int) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value) // 100}.{abs(value) % 100:02d}"


def _qb_name(account: str) -> str:
    return account.split(":")[-1].replace("-", " ")


def _qb_type(account: str) -> str:
    return "Income" if account.startswith("Income:") else "Expenses"


def build(seed: int, rows: int) -> dict:
    """Return the full synthetic dataset as plain data."""
    if rows < 1:
        raise ValueError("rows must be at least 1")
    rng = random.Random(seed)
    vendors = INCOME_VENDORS + EXPENSE_VENDORS
    covered = {v[0] for v in INCOME_VENDORS} | {v[0] for v in EXPENSE_VENDORS[:EXPENSE_COVERED]}
    rule_account = {v[0]: v[1] for v in vendors}
    rule_account[WRONG_RULE_VENDOR] = WRONG_RULE_ACCOUNT

    span = (PERIOD_TO - PERIOD_FROM).days
    txns: list[dict] = []
    for index in range(rows):
        vendor, account, low, high = (
            rng.choice(INCOME_VENDORS) if rng.random() < 0.2 else rng.choice(EXPENSE_VENDORS)
        )
        cents = rng.randrange(low, high + 1)
        signed = cents if account.startswith("Income:") else -cents
        day = PERIOD_FROM + timedelta(days=rng.randrange(span + 1))
        txns.append({
            "id": f"syn-{seed}-{index:04d}",
            "date": day,
            "vendor": vendor,
            "description": f"{vendor} ref {index:04d}",
            "cents": signed,
            "truth": account,
        })
    txns.sort(key=lambda t: (t["date"], t["id"]))

    posted = [t for t in txns if t["vendor"] in covered]
    wrong = [t for t in posted if rule_account[t["vendor"]] != t["truth"]]
    manifest = {
        "seed": seed,
        "rows": rows,
        "auto_posted": len(posted),
        "pending": rows - len(posted),
        "agree": len(posted) - len(wrong),
        "disagree": len(wrong),
    }
    rules = [
        {"match": "prefix", "pattern": vendor, "account": rule_account[vendor]}
        for vendor in sorted(covered)
    ]
    return {"txns": txns, "rules": rules, "manifest": manifest}


def _banksync(txns: list[dict]) -> dict:
    rows = []
    for t in txns:
        amount = _cents(t["cents"])
        credit = t["cents"] > 0
        rows.append({
            "id": t["id"], "date": str(t["date"]), "authorizedDate": str(t["date"]),
            "description": t["description"], "originalDescription": t["description"].upper(),
            "amount": amount,
            "creditAmount": amount if credit else None,
            "debitAmount": None if credit else _cents(-t["cents"]),
            "currency": "USD", "category": "income" if credit else "expense",
            "type": "credit" if credit else "debit", "reference": t["id"],
            "pending": False, "pendingTransactionId": None,
            "accountId": "acct-synthetic-1", "accountName": "Synthetic Checking",
            "accountNumberLast4": "0000", "bankId": "bank-1", "bank": "Synthetic Bank",
        })
    return {
        "source": "banksync", "bankNameFilter": "synthetic", "scope": "self",
        "dateRange": {"from": str(PERIOD_FROM), "to": str(PERIOD_TO)},
        "connectedBankCount": 1,
        "connectedBanks": [{"id": "bank-1", "name": "Synthetic Bank", "source": "plaid",
                            "type": "bank", "status": "connected"}],
        "matchedBankCount": 1,
        "banks": [{
            "bank": {"id": "bank-1", "name": "Synthetic Bank", "source": "plaid",
                     "type": "bank", "status": "connected", "institutionId": "synthetic",
                     "createdAt": None, "updatedAt": None, "workspaceId": None,
                     "portalName": None},
            "accountCount": 1,
            "accounts": [{
                "account": {"id": "acct-synthetic-1", "bankId": "bank-1",
                            "accountName": "Synthetic Checking", "accountType": "checking",
                            "currency": "USD", "balance": _cents(sum(t["cents"] for t in txns)),
                            "availableBalance": _cents(sum(t["cents"] for t in txns)),
                            "accountNumberLast4": "0000", "createdAt": None, "updatedAt": None},
                "transactions": rows,
            }],
        }],
    }


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


def _write_qb_export(folder: Path, txns: list[dict]) -> None:
    """Write a QuickBooks cash-basis export with the truth categories."""
    folder.mkdir(parents=True, exist_ok=True)
    by_account: dict[str, list[dict]] = defaultdict(list)
    for t in txns:
        by_account[t["truth"]].append(t)
    income = sorted(a for a in by_account if a.startswith("Income:"))
    expenses = sorted(a for a in by_account if a.startswith("Expenses:"))
    totals = {a: sum(abs(t["cents"]) for t in by_account[a]) for a in by_account}
    income_total = sum(totals[a] for a in income)
    expense_total = sum(totals[a] for a in expenses)
    net = income_total - expense_total
    bank_total = sum(t["cents"] for t in txns)
    period = "January-March, 2026"
    stamp = "Cash Basis Monday, April 6, 2026 10:00 AM GMT-05:00"

    _write_csv(folder / "chart_of_accounts.csv",
               [["Account name", "Account type", "Detail type", "Lock"],
                ["Checking", "Bank", "Checking", "No"]]
               + [[_qb_name(a), _qb_type(a), "Other", "No"] for a in income + expenses]
               + [["Retained Earnings", "Equity", "Retained Earnings", "No"]])

    pnl = [[COMPANY, ""], ["Profit and Loss", ""], [period, ""], [], ["", "Total"], ["Income", ""]]
    pnl += [[_qb_name(a), _cents(totals[a])] for a in income]
    pnl += [["Total for Income", f"${_cents(income_total)}"],
            ["Gross Profit", f"${_cents(income_total)}"], ["Expenses", ""]]
    pnl += [[_qb_name(a), _cents(totals[a])] for a in expenses]
    pnl += [["Total for Expenses", f"${_cents(expense_total)}"],
            ["Net Income", f"${_cents(net)}"], [], [], [], [stamp, ""]]
    _write_csv(folder / "profit_and_loss.csv", pnl)

    _write_csv(folder / "balance_sheet_2026_03_31.csv", [
        [COMPANY, ""], ["Balance Sheet", ""], ["As of Mar 31, 2026", ""], [], ["", "Total"],
        ["Assets", ""], ["Current Assets", ""], ["Bank Accounts", ""],
        ["Checking", _cents(bank_total)],
        ["Total for Bank Accounts", f"${_cents(bank_total)}"],
        ["Total for Current Assets", f"${_cents(bank_total)}"],
        ["Total for Assets", f"${_cents(bank_total)}"],
        ["Liabilities and Equity", ""], ["Liabilities", ""], ["Total for Liabilities", "$0.00"],
        ["Equity", ""], ["Retained Earnings", "$0.00"], ["Net Income", _cents(net)],
        ["Total for Equity", f"${_cents(net)}"],
        ["Total for Liabilities and Equity", f"${_cents(net)}"], [], [], [], [stamp, ""]])

    tb = [[COMPANY, "", ""], ["Trial Balance", "", ""], ["As of Mar 31, 2026", "", ""], [],
          ["Full name", "Debit", "Credit"],
          ["Checking", _cents(bank_total) if bank_total >= 0 else "",
           _cents(-bank_total) if bank_total < 0 else ""]]
    tb += [[_qb_name(a), "", _cents(totals[a])] for a in income]
    tb += [[_qb_name(a), _cents(totals[a]), ""] for a in expenses]
    debit = (bank_total if bank_total >= 0 else 0) + expense_total
    credit = income_total + (-bank_total if bank_total < 0 else 0)
    tb += [["TOTAL", f"${_cents(debit)}", f"${_cents(credit)}"], [], [], [], [stamp, "", ""]]
    _write_csv(folder / "trial_balance_cash_basis.csv", tb)

    gl = [[COMPANY] + [""] * 8, ["General Ledger"] + [""] * 8, [period] + [""] * 8, [],
          ["", "Transaction date", "Transaction type", "Num", "Name", "Description", "Split",
           "Amount", "Balance"],
          ["Checking"] + [""] * 8, [""] * 8 + ["0.00"]]

    def line(t: dict, split: str, amount: int, balance: int) -> list[str]:
        kind = "Deposit" if t["cents"] > 0 else "Expense"
        return ["", t["date"].strftime("%m/%d/%Y"), kind, "", t["vendor"], t["description"],
                split, _cents(amount), _cents(balance)]

    running = 0
    for t in txns:
        running += t["cents"]
        gl.append(line(t, _qb_name(t["truth"]), t["cents"], running))
    gl.append(["Total for Checking"] + [""] * 7 + [f"${_cents(bank_total)}"])
    for account in income + expenses:
        gl.append([_qb_name(account)] + [""] * 8)
        gl.append([""] * 8 + ["0.00"])
        running = 0
        for t in by_account[account]:
            amount = -t["cents"] if account.startswith("Income:") else abs(t["cents"])
            running += amount
            gl.append(line(t, "Checking", amount, running))
        gl.append([f"Total for {_qb_name(account)}"] + [""] * 6 + [f"${_cents(running)}", ""])
    gl += [[], [], [], [stamp] + [""] * 8]
    _write_csv(folder / "general_ledger.csv", gl)


def write_dataset(out_dir: Path, seed: int, rows: int) -> dict:
    """Write all files under *out_dir* and return the manifest."""
    data = build(seed, rows)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "banksync.json").write_text(
        json.dumps(_banksync(data["txns"]), indent=2) + "\n", encoding="utf-8")
    _write_csv(out_dir / "truth.csv",
               [["source_id", "account"]] + [[t["id"], t["truth"]] for t in data["txns"]])
    _write_qb_export(out_dir / "qb", data["txns"])
    entity = {
        "name": COMPANY,
        "business_type": "consulting",
        "category_rules": data["rules"],
        "bank_account_mappings": {"Synthetic Checking": BANK_ACCOUNT},
    }
    (out_dir / "entity.json").write_text(json.dumps(entity, indent=2) + "\n", encoding="utf-8")
    (out_dir / "manifest.json").write_text(
        json.dumps(data["manifest"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return data["manifest"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--rows", type=int, default=120)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(write_dataset(args.out, args.seed, args.rows), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
