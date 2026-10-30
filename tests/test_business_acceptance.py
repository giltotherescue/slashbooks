"""Independent joined CLI acceptance with real sealed synthetic company books."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from bookkeeping.cli import main
from bookkeeping.entity import add_account, init_entity, load_entity
from bookkeeping.ledger.importer import _atomic_ledger_write, check_integrity
from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.ledger.store import LedgerStore


BANK = "Assets:Bank:Checking"
AR = "Assets:Receivable:Customers"
AP = "Liabilities:Payable:Vendors"
INCOME = "Income:Consulting"
EXPENSE = "Expenses:Software"
FEES = "Expenses:Payment-Fees"
TAX = "Liabilities:Sales-Tax"


class BusinessCliAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="books-business-acceptance-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.sequence = 0

    def cli(self, *argv, status=0):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            actual = main([str(arg) for arg in argv])
        self.assertEqual(actual, status, f"books {' '.join(map(str, argv))}\n{output.getvalue()}\n{errors.getvalue()}")
        try:
            return json.loads(output.getvalue())
        except json.JSONDecodeError:
            self.fail(f"Command returned non-JSON: {output.getvalue()}\n{errors.getvalue()}")

    def input(self, payload):
        self.sequence += 1
        target = self.directory / f"input-{self.sequence}.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def business(self, root, verb, payload=None, status=0, *extra):
        argv = ["business", verb, "--entity", root]
        if payload is not None:
            argv.extend(["--input", self.input(payload)])
        return self.cli(*argv, *extra, status=status)

    def change(self, root, payload):
        plan = self.business(root, "preview", payload)
        result = self.business(root, "apply", plan)
        before = LedgerStore(root / "ledger.sqlite").content_digest()
        replay = self.business(root, "apply", plan)
        self.assertEqual(replay, {**result, "status": "already_applied"})
        self.assertEqual(LedgerStore(root / "ledger.sqlite").content_digest(), before)
        self.assertEqual(check_integrity(load_entity(root)).status, "ok")
        return result

    def fixture_post(self, root, identity, when, postings):
        """Prepare genuinely recorded history; workflows themselves use the CLI."""
        entry = Entry(date.fromisoformat(when), "Synthetic pre-existing source",
                      meta=(("source-id", identity),),
                      postings=tuple(Posting(account, Decimal(amount)) for account, amount in postings))
        _atomic_ledger_write(load_entity(root), [], [entry], identity, None, "Synthetic source fixture")

    def company(self, basis, *, history=False, closed_through=None):
        root = self.directory / f"company-{basis}-{self.sequence}"
        init_entity(root, name="Synthetic acceptance company", basis=basis)
        for account in (BANK, AR, AP, INCOME, EXPENSE, FEES, TAX):
            add_account(root, account, open_date="2000-01-01")
        if history:
            self.fixture_post(root, "historical-sale", "2025-12-15", [(BANK, "250.00"), (INCOME, "-250.00")])
        if closed_through:
            config_path = root / "entity.json"
            config = json.loads(config_path.read_text())
            config["closed_through"] = closed_through
            config_path.write_text(json.dumps(config))
        self.change(root, {"operation_id": "start-business", "action": "tracking.enable",
                           "tracking_start": "2026-02-01" if closed_through else "2026-01-01"})
        self.change(root, {"operation_id": "save-party", "action": "party.save", "id": "party-a",
                           "expected_revision": 0, "display_name": "Synthetic Services",
                           "roles": ["customer", "vendor"], "email": "synthetic@example.invalid"})
        return root

    def evidence(self, root, identity):
        payload = {"schema_version": 1, "source": {"namespace": "acceptance.synthetic", "account_id": "synthetic-bank",
                   "object_id": identity, "version": "1"}, "currency": "USD",
                   "observed_facts": {"description": "Synthetic receipt or remittance"}}
        return self.cli("accrual", "intake", "--entity", root, "--input", self.input(payload))["id"]

    def draft(self, root, identity="invoice-a", *, kind="invoice", amount="1000.00", tax="0.00", when="2026-01-01"):
        payload = {"operation_id": identity + "-draft", "action": "document.save", "id": identity,
                   "expected_revision": 0, "kind": kind, "party_id": "party-a", "date": when,
                   "due_date": "2026-03-01", "currency": "USD", "control_account": AR if kind == "invoice" else AP,
                   "lines": [{"description": "Synthetic services", "quantity": "1", "unit_price": amount,
                              "account": INCOME if kind == "invoice" else EXPENSE,
                              "tax_amount": tax, "tax_account": TAX}]}
        self.change(root, payload)
        return payload

    def issue(self, root, identity="invoice-a"):
        return self.change(root, {"operation_id": identity + "-issue", "action": "document.issue", "id": identity,
                                  "expected_revision": 1})

    def payment(self, root, identity, document, amount, *, direction="receive", source=None, **extra):
        return {"operation_id": identity, "action": "payment.record", "id": identity, "party_id": "party-a",
                "date": "2026-01-15", "currency": "USD", "direction": direction, "gross_amount": amount,
                "cash_amount": amount, "federal_withholding": "0.00", "payment_method": "bank_transfer",
                "source": source or {"mode": "new", "source_id": identity + "-source", "bank_account": BANK,
                                      "amount": ("-" if direction == "pay" else "") + amount},
                "allocations": [{"item_id": document, "amount": amount, "tax_category": "unknown",
                                 "classification_verified": False}], "evidence_ids": [self.evidence(root, identity)], **extra}

    def balances(self, root):
        result = {}
        for entry in LedgerStore(root / "ledger.sqlite").load_entries():
            for posting in entry.postings:
                result[posting.account] = result.get(posting.account, Decimal(0)) + posting.amount
        return result

    def pnl(self, root, basis):
        return self.cli("report", "pnl", "--entity", root, "--from", "2026-01-01", "--to", "2026-01-31",
                        "--basis", basis, "--format", "json")

    def records(self, root, resource, **filters):
        argv = ["business", "records", "--entity", root, "--resource", resource]
        for name, value in filters.items():
            argv.extend(["--" + name.replace("_", "-"), value])
        return self.cli(*argv)["records"]

    def test_cash_and_accrual_joined_partial_invoice_bill_and_reports(self):
        for basis in ("cash", "accrual"):
            with self.subTest(basis=basis):
                root = self.company(basis, history=True)
                store = LedgerStore(root / "ledger.sqlite")
                before = [(e.source_id, e.date, e.postings) for e in store.load_entries()]
                self.draft(root, tax="100.00")
                self.assertEqual([(e.source_id, e.date, e.postings) for e in store.load_entries()], before)
                self.issue(root)
                self.change(root, self.payment(root, "half-invoice", "invoice-a", "550.00"))
                self.draft(root, "bill-a", kind="bill", amount="1200.00")
                self.issue(root, "bill-a")
                self.change(root, self.payment(root, "half-bill", "bill-a", "600.00", direction="pay"))
                self.assertEqual(load_entity(root).entity_config["basis"], basis)
                self.assertEqual(self.balances(root)[BANK], Decimal("200.00"))
                self.assertEqual(self.balances(root)[AR], Decimal("550.00"))
                self.assertEqual(self.balances(root)[AP], Decimal("-600.00"))
                self.assertEqual(self.balances(root)[TAX], Decimal("-100.00"))
                self.assertEqual(self.balances(root)[INCOME], Decimal("-1250.00"))
                self.assertEqual(self.balances(root)[EXPENSE], Decimal("1200.00"))
                self.assertEqual(Decimal(self.pnl(root, "cash")["totals"]["net_income"]), Decimal("-100.00"))
                self.assertEqual(Decimal(self.pnl(root, "accrual")["totals"]["net_income"]), Decimal("-200.00"))
                documents = {d["id"]: d for d in self.records(root, "documents", as_of="2026-02-01")}
                self.assertEqual(documents["invoice-a"]["remaining_amount"], "550.00")
                self.assertEqual(documents["bill-a"]["remaining_amount"], "600.00")
                self.assertEqual({d["status"] for d in documents.values()}, {"partially_paid"})
                party = self.records(root, "parties")[0]
                self.assertEqual(party["balances"], {"receivable": "550.00", "payable": "600.00",
                                                   "customer_credit": "0.00", "vendor_credit": "0.00",
                                                   "net_receivable": "550.00", "net_payable": "600.00"})
                self.assertEqual(len(self.records(root, "payments")), 2)
                aging = self.cli("accrual", "report", "--entity", root, "--kind", "aging", "--as-of", "2026-03-15")
                self.assertEqual(aging["totals"]["receivable"]["buckets"]["1_30"], "550.00")
                self.assertEqual(aging["totals"]["payable"]["buckets"]["1_30"], "600.00")
                self.assertEqual(aging["totals"]["receivable"]["overdue_count"], 1)
                self.assertEqual(aging["totals"]["payable"]["overdue_count"], 1)
                historical = next(e for e in store.load_entries() if e.source_id == "historical-sale")
                self.assertEqual((historical.date, historical.postings), (before[0][1], before[0][2]))

    def test_existing_processor_receipt_keeps_gross_fee_and_cash_once(self):
        root = self.company("cash")
        self.draft(root, amount="100.00")
        self.issue(root)
        self.fixture_post(root, "processor-existing", "2026-01-15", [(BANK, "97.00"), (FEES, "3.00"), (INCOME, "-100.00")])
        payload = self.payment(root, "link-processor", "invoice-a", "100.00",
                               source={"mode": "existing", "source_id": "processor-existing", "bank_account": BANK, "amount": "97.00"},
                               fee_amount="3.00", fee_account=FEES)
        self.change(root, payload)
        self.assertEqual(self.balances(root)[BANK], Decimal("97.00"))
        self.assertEqual(self.balances(root)[FEES], Decimal("3.00"))
        self.assertEqual(self.balances(root)[INCOME], Decimal("-100.00"))
        self.assertEqual(self.balances(root)[AR], Decimal(0))
        self.assertEqual(Decimal(self.pnl(root, "cash")["totals"]["net_income"]), Decimal("97.00"))
        self.assertEqual(self.records(root, "documents", as_of="2026-01-31")[0]["status"], "paid")
        self.assertEqual(sum(e.source_id == "processor-existing" for e in LedgerStore(root / "ledger.sqlite").load_entries()), 1)

    def test_stale_party_preview_does_not_replace_current_contact(self):
        root = self.company("cash")
        change = {"operation_id": "edit-party-a", "action": "party.save", "id": "party-a", "expected_revision": 1,
                  "display_name": "Outdated edit", "roles": ["customer", "vendor"]}
        stale = self.business(root, "preview", change)
        self.change(root, {**change, "operation_id": "edit-party-b", "display_name": "Current contact"})
        before = LedgerStore(root / "ledger.sqlite").content_digest()
        refused = self.business(root, "apply", stale, status=1)
        self.assertTrue(refused.get("errors"))
        self.assertEqual(LedgerStore(root / "ledger.sqlite").content_digest(), before)
        self.assertEqual(self.records(root, "parties")[0]["display_name"], "Current contact")

    def test_initial_business_reads_are_empty_without_schema_side_effects(self):
        for basis in ("cash", "accrual"):
            with self.subTest(basis=basis):
                root = self.directory / f"untracked-{basis}"
                init_entity(root, basis=basis)
                store = LedgerStore(root / "ledger.sqlite")
                schema, digest = store.schema_version(), store.content_digest()
                for resource in ("parties", "documents", "payments", "tax_preparations"):
                    response = self.cli("business", "records", "--entity", root, "--resource", resource)
                    self.assertEqual(response["records"], [])
                    self.assertTrue(response["tracking_required"])
                self.assertEqual(store.schema_version(), schema)
                self.assertEqual(store.content_digest(), digest)
                self.assertEqual(load_entity(root).entity_config["basis"], basis)

    def test_source_candidates_page_without_loss_and_reject_changed_state(self):
        root = self.company("cash")
        entries = [Entry(date(2026, 1, 15), "Synthetic unclassified vendor payment",
                         meta=(("source-id", f"candidate-{index:02}"),),
                         postings=(Posting(BANK, Decimal("-10.00")), Posting(EXPENSE, Decimal("10.00"))))
                   for index in range(31)]
        _atomic_ledger_write(load_entity(root), [], entries, "candidate-fixture", None, "Synthetic source fixtures")
        base = ["business", "records", "--entity", root, "--resource", "payment_candidates", "--limit", "7"]
        first = self.cli(*base)
        self.assertEqual((first["total"], len(first["records"])), (31, 7))
        records, cursor = list(first["records"]), first["next_cursor"]
        while cursor is not None:
            page = self.cli(*base, "--cursor", cursor)
            self.assertEqual(page["total"], 31)
            self.assertEqual(page["ledger_digest"], first["ledger_digest"])
            self.assertLessEqual(len(page["records"]), 7)
            records.extend(page["records"])
            cursor = page["next_cursor"]
        self.assertEqual(len(records), 31)
        self.assertEqual({row["source_id"] for row in records}, {f"candidate-{index:02}" for index in range(31)})
        changed_filter = self.cli(*base, "--cursor", first["next_cursor"], "--as-of", "2026-01-31", status=1)
        self.assertEqual(changed_filter["errors"][0]["code"], "stale_record_cursor")
        self.fixture_post(root, "new-candidate", "2026-01-16", [(BANK, "-15.00"), (EXPENSE, "15.00")])
        stale = self.cli(*base, "--cursor", first["next_cursor"], status=1)
        self.assertEqual(stale["errors"][0]["code"], "stale_record_cursor")
        fresh = self.cli(*base)
        self.assertEqual(fresh["total"], 32)

    def test_facts_only_payment_link_cannot_invent_withholding(self):
        root = self.company("cash")
        self.fixture_post(root, "paid-eight-hundred", "2026-01-15", [(BANK, "-800.00"), (EXPENSE, "800.00")])
        request = self.payment(root, "unsupported-withholding", "unused", "1000.00", direction="pay",
                               source={"mode": "existing", "source_id": "paid-eight-hundred",
                                       "bank_account": BANK, "amount": "-800.00"})
        request.update(action="payment.link", cash_amount="800.00", federal_withholding="200.00",
                       allocations=[{"amount": "1000.00", "tax_category": "nonemployee_compensation",
                                     "classification_verified": True}])
        before = LedgerStore(root / "ledger.sqlite").content_digest()
        refused = self.business(root, "preview", request, status=1)
        self.assertTrue(refused["errors"])
        self.assertEqual(LedgerStore(root / "ledger.sqlite").content_digest(), before)
        self.assertEqual(self.records(root, "payments"), [])

    def test_dated_records_do_not_call_future_invoices_paid(self):
        root = self.company("cash")
        self.draft(root, tax="100.00")
        self.issue(root)
        self.change(root, self.payment(root, "later-payment", "invoice-a", "550.00"))
        self.assertEqual(self.records(root, "documents", as_of="2025-12-31"), [])
        self.assertEqual(self.records(root, "payments", as_of="2026-01-14"), [])
        january_first = self.records(root, "documents", as_of="2026-01-01")[0]
        self.assertEqual(january_first["status"], "open")
        self.assertEqual(january_first["remaining_amount"], "1100.00")
        self.assertEqual(self.records(root, "parties", as_of="2025-12-31")[0]["balances"]["receivable"], "0.00")
        self.assertEqual(self.records(root, "parties", as_of="2026-01-01")[0]["balances"]["receivable"], "1100.00")
        self.assertEqual(self.records(root, "parties", as_of="2026-01-15")[0]["balances"]["receivable"], "550.00")

    def test_dated_records_do_not_leak_later_void_or_writeoff_status(self):
        for action, final in (("document.void", "void"), ("document.writeoff", "uncollectible")):
            with self.subTest(action=action):
                root = self.company("cash")
                self.draft(root)
                self.issue(root)
                correction = {"operation_id": "later-correction", "action": action, "id": "invoice-a",
                              "expected_revision": 2, "date": "2026-01-31", "reason": "Synthetic later correction",
                              "evidence_ids": [self.evidence(root, "later-correction-support")]}
                if action == "document.writeoff":
                    correction["counter_account"] = EXPENSE
                self.change(root, correction)
                before = self.records(root, "documents", as_of="2026-01-15")[0]
                self.assertEqual((before["status"], before["remaining_amount"]), ("open", "1000.00"))
                after = self.records(root, "documents", as_of="2026-01-31")[0]
                self.assertEqual((after["status"], after["remaining_amount"]), (final, "0.00"))

    def test_partial_reduction_cannot_mark_an_invoice_void(self):
        root = self.company("cash")
        self.draft(root)
        self.issue(root)
        evidence = self.evidence(root, "partial-void-support")
        before = LedgerStore(root / "ledger.sqlite").content_digest()
        refused = self.business(root, "preview", {"operation_id": "partial-void", "action": "document.void",
                                                "id": "invoice-a", "expected_revision": 2, "date": "2026-01-31",
                                                "amount": "100.00", "reason": "Synthetic partial cancellation",
                                                "evidence_ids": [evidence]}, status=1)
        self.assertTrue(refused["errors"])
        self.assertEqual(LedgerStore(root / "ledger.sqlite").content_digest(), before)
        self.assertEqual(self.records(root, "documents", as_of="2026-01-31")[0]["remaining_amount"], "1000.00")

    def test_tracking_retains_prior_period_lock_and_blocks_old_issue(self):
        root = self.company("cash", history=True, closed_through="2026-01-31")
        self.assertEqual(load_entity(root).entity_config["closed_through"], "2026-01-31")
        self.draft(root, when="2026-01-10")
        before = LedgerStore(root / "ledger.sqlite").content_digest()
        refused = self.business(root, "preview", {"operation_id": "closed-issue", "action": "document.issue",
                                                "id": "invoice-a", "expected_revision": 1}, status=1)
        self.assertEqual(refused["errors"][0]["code"], "closed_period")
        self.assertEqual(LedgerStore(root / "ledger.sqlite").content_digest(), before)
        self.assertEqual(self.records(root, "documents")[0]["status"], "draft")

    def test_existing_source_continuation_preserves_legacy_claim_hash(self):
        root = self.company("accrual")
        self.draft(root)
        self.issue(root)
        self.fixture_post(root, "prior-bank-source", "2026-01-15", [(BANK, "1000.00"), (INCOME, "-1000.00")])
        evidence = self.evidence(root, "legacy-remittance")
        def settle(identity, amount):
            request = {"operation_id": identity, "date": "2026-01-15", "source": {"mode": "existing",
                       "source_id": "prior-bank-source", "bank_account": BANK, "amount": "1000.00"},
                       "allocations": [{"item_id": "invoice-a", "amount": amount}], "evidence_ids": [evidence]}
            plan = self.cli("accrual", "item", "settle-preview", "--entity", root, "--input", self.input(request))
            return self.cli("accrual", "item", "settle-apply", "--entity", root, "--input", self.input(plan))
        settle("legacy-first", "400.00")
        # v0.9.2 source identity has no optional empty components field. Assert
        # the historical serialized contract independently, without rewriting state.
        expected = {"kind": "existing_payment", "source_id": "prior-bank-source", "date": "2026-01-15",
                    "bank_account": BANK, "amount": "1000.00"}
        digest = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        store = LedgerStore(root / "ledger.sqlite")
        with store.connection() as conn:
            claims = [json.loads(row[0]) for row in conn.execute("SELECT payload_json FROM accrual_intents")]
        payment_claims = [row for row in claims if row.get("kind") == "source_claim" and row.get("operation_id") == "legacy-first"]
        self.assertEqual(len(payment_claims), 1)
        self.assertEqual(payment_claims[0]["financial_hash"], digest)
        settle("legacy-remaining", "600.00")
        self.assertEqual(self.balances(root)[BANK], Decimal("1000.00"))
        self.assertEqual(self.balances(root)[INCOME], Decimal("-1000.00"))
        self.assertEqual(self.balances(root)[AR], Decimal(0))
        self.assertEqual(check_integrity(load_entity(root)).status, "ok")


if __name__ == "__main__":
    unittest.main()
