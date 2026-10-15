"""OFX, QFX and QBO (Web Connect) statement reader.

QFX and QBO Web Connect files are OFX files with an Intuit header field, so
one reader handles all three. Both OFX 1.x (SGML, leaf tags without closing
tags) and OFX 2.x (XML) are supported. The reader does not use an XML parser,
so it does not resolve entities or load external content.

For each bank or card statement in the file the reader returns:

- account identity: institution ID, account type, the last four account
  characters, and a one-way identity hash (the full account number is not
  kept);
- statement period (``DTSTART``/``DTEND``) and ledger balance
  (``LEDGERBAL``) when present;
- transactions with their ``FITID``.

``TRNAMT`` is signed from the account holder's view (credits positive, debits
negative) for bank and card statements, which is the same axis as the
normalized transaction contract.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .bankcsv import MONEY, StatementFileError

JsonObject = dict[str, Any]

_TAG = re.compile(r"<(/?)([A-Za-z0-9.]+)>([^<]*)")


@dataclass
class Node:
    name: str
    text: str = ""
    children: list["Node"] = field(default_factory=list)

    def find(self, name: str) -> "Node | None":
        for child in self.children:
            if child.name == name:
                return child
        return None

    def find_all(self, name: str) -> list["Node"]:
        return [child for child in self.children if child.name == name]

    def walk(self, name: str) -> list["Node"]:
        found = []
        for child in self.children:
            if child.name == name:
                found.append(child)
            found.extend(child.walk(name))
        return found

    def value(self, name: str) -> str:
        child = self.find(name)
        return child.text.strip() if child is not None else ""


@dataclass
class OfxTransaction:
    fitid: str
    date: str
    amount: Decimal
    name: str
    memo: str
    trntype: str
    reference: str


@dataclass
class OfxStatement:
    kind: str  # "bank" or "creditcard"
    institution: str
    account_type: str
    account_last4: str
    account_identity: str
    currency: str
    period_start: str | None
    period_end: str | None
    ledger_balance: Decimal | None
    ledger_balance_date: str | None
    transactions: list[OfxTransaction]
    in_file_duplicates: int = 0


def looks_like_ofx(text: str) -> bool:
    head = text[:2048].upper()
    return "OFXHEADER" in head or "<OFX>" in head or "<?OFX" in head


def _tree(text: str) -> Node:
    start = text.upper().find("<OFX>")
    if start < 0:
        raise StatementFileError("malformed_ofx", "The file has no <OFX> element.")
    body = text[start:]
    closed = {name.upper() for name in re.findall(r"</([A-Za-z0-9.]+)>", body)}
    root = Node("ROOT")
    stack = [root]
    for match in _TAG.finditer(body):
        closing, name, raw = match.group(1), match.group(2).upper(), match.group(3)
        if closing:
            # Close the named aggregate; tolerate closing tags for SGML leaves.
            for depth in range(len(stack) - 1, 0, -1):
                if stack[depth].name == name:
                    del stack[depth:]
                    break
            continue
        node = Node(name, _unescape(raw.strip()))
        stack[-1].children.append(node)
        if not node.text and name in closed:
            stack.append(node)
    if len(stack) > 1:
        raise StatementFileError("malformed_ofx",
                                 f"The OFX file is truncated: <{stack[-1].name}> is not closed.")
    ofx = root.find("OFX")
    if ofx is None:
        raise StatementFileError("malformed_ofx", "The file has no <OFX> element.")
    return ofx


def _unescape(value: str) -> str:
    return (value.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&apos;", "'").replace("&nbsp;", " ").replace("&amp;", "&"))


def _ofx_date(value: str, label: str) -> str:
    digits = value.strip()[:8]
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
    except (ValueError, IndexError) as exc:
        raise StatementFileError("malformed_ofx", f"{label} {value!r} is not an OFX date.") from exc


def _ofx_amount(value: str, label: str) -> Decimal:
    text = value.strip().replace(",", ".") if value.count(",") == 1 and "." not in value else value.strip()
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise StatementFileError("malformed_ofx", f"{label} {value!r} is not a number.") from exc
    return amount.quantize(MONEY)


def identity_hash(institution: str, account_id: str, account_type: str) -> str:
    payload = f"{institution.strip().upper()}|{account_id.strip().upper()}|{account_type.strip().upper()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def parse_ofx(text: str) -> list[OfxStatement]:
    """Parse every bank and card statement in an OFX/QFX/QBO document."""
    ofx = _tree(text)
    statements: list[OfxStatement] = []
    for kind, wrapper, acct_tag in (("bank", "STMTRS", "BANKACCTFROM"),
                                    ("creditcard", "CCSTMTRS", "CCACCTFROM")):
        for stmt in ofx.walk(wrapper):
            statements.append(_statement(stmt, kind, acct_tag))
    if not statements:
        if ofx.walk("INVSTMTRS"):
            raise StatementFileError("unsupported_statement",
                                     "Investment statements are not supported; import bank and "
                                     "card statements only.")
        raise StatementFileError("malformed_ofx", "The OFX file contains no bank or card statement.")
    return statements


def _statement(stmt: Node, kind: str, acct_tag: str) -> OfxStatement:
    currency = stmt.value("CURDEF").upper() or "USD"
    if currency != "USD":
        raise StatementFileError("unsupported_currency",
                                 f"The statement currency is {currency}. Slashbooks books only USD activity.")
    acct = stmt.find(acct_tag)
    if acct is None:
        raise StatementFileError("malformed_ofx", f"The statement has no {acct_tag} account element.")
    account_id = acct.value("ACCTID")
    if not account_id:
        raise StatementFileError("malformed_ofx", "The statement has no ACCTID.")
    institution = acct.value("BANKID")
    account_type = acct.value("ACCTTYPE").upper() or ("CREDITCARD" if kind == "creditcard" else "")
    tranlist = stmt.find("BANKTRANLIST")
    period_start = period_end = None
    transactions: list[OfxTransaction] = []
    duplicates = 0
    seen: dict[str, OfxTransaction] = {}
    if tranlist is not None:
        if tranlist.value("DTSTART"):
            period_start = _ofx_date(tranlist.value("DTSTART"), "DTSTART")
        if tranlist.value("DTEND"):
            period_end = _ofx_date(tranlist.value("DTEND"), "DTEND")
        for index, node in enumerate(tranlist.find_all("STMTTRN"), start=1):
            fitid = node.value("FITID")
            if not fitid:
                raise StatementFileError("malformed_ofx", f"Transaction {index} has no FITID.")
            cur = node.find("CURRENCY")
            if cur is not None and cur.value("CURSYM").upper() not in {"", "USD"}:
                raise StatementFileError(
                    "unsupported_currency",
                    f"Transaction {fitid} is in {cur.value('CURSYM')}. Slashbooks books only USD activity.")
            name = node.value("NAME")
            payee = node.find("PAYEE")
            if not name and payee is not None:
                name = payee.value("NAME")
            txn = OfxTransaction(
                fitid=fitid,
                date=_ofx_date(node.value("DTPOSTED") or node.value("DTUSER"), f"Transaction {fitid} date"),
                amount=_ofx_amount(node.value("TRNAMT"), f"Transaction {fitid} amount"),
                name=name,
                memo=node.value("MEMO"),
                trntype=node.value("TRNTYPE").upper(),
                reference=node.value("CHECKNUM") or node.value("REFNUM"),
            )
            if fitid in seen:
                if seen[fitid] == txn:
                    duplicates += 1
                    continue
                raise StatementFileError(
                    "conflicting_fitid",
                    f"FITID {fitid} appears twice with different details; the file is not reliable.")
            seen[fitid] = txn
            transactions.append(txn)
    ledger = stmt.find("LEDGERBAL")
    balance = balance_date = None
    if ledger is not None and ledger.value("BALAMT"):
        balance = _ofx_amount(ledger.value("BALAMT"), "LEDGERBAL")
        if ledger.value("DTASOF"):
            balance_date = _ofx_date(ledger.value("DTASOF"), "LEDGERBAL DTASOF")
    return OfxStatement(
        kind=kind,
        institution=institution,
        account_type=account_type,
        account_last4=account_id[-4:],
        account_identity=identity_hash(institution, account_id, account_type),
        currency=currency,
        period_start=period_start,
        period_end=period_end,
        ledger_balance=balance,
        ledger_balance_date=balance_date,
        transactions=transactions,
        in_file_duplicates=duplicates,
    )
