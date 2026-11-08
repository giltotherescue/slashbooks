"""Synthetic engine acceptance for an owner's persistent chart choices.

This exercises supported operations, not a model or the hosted role boundary.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from bookkeeping.entity import add_account, apply_starter_accounts, init_entity, load_entity, starter_chart
from bookkeeping.ledger.importer import _atomic_ledger_write, check_integrity
from bookkeeping.ledger.model import Entry, Posting
from bookkeeping.ledger.store import LedgerStore
from bookkeeping.taxmap import update_settings


class OwnerChartJourneyTests(unittest.TestCase):
    def test_business_defaults_custom_hierarchy_numbers_and_posted_history_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "company"
            init_entity(root, name="Synthetic Studio", business_type="consulting", cutover_date="2026-01-01")
            store = LedgerStore(root / "ledger.sqlite")
            self.assertTrue(set(starter_chart("consulting")) <= store.load_account_names())
            chosen = "Income:Design:Retainers"
            add_account(root, chosen, open_date="2026-01-01")
            update_settings(root, account=chosen, account_number="4100.01")
            # A later onboarding answer can change unused defaults, not the owner's addition.
            apply_starter_accounts(root, "services")
            self.assertIn(chosen, store.load_account_names())
            entry = Entry(date=date(2026, 2, 1), narration="Synthetic retainer",
                          meta=(("source-id", "synthetic-retainer-1"),), postings=(
                              Posting("Assets:Bank:Checking", Decimal("100")),
                              Posting(chosen, Decimal("-100"))))
            _atomic_ledger_write(load_entity(root), [], [entry], "chart-acceptance", None, "synthetic")
            before = store.content_digest()
            # Exact account creation replay and reload retain the owner's choice.
            self.assertEqual(add_account(root, chosen, open_date="2026-01-01")["status"], "existed")
            self.assertEqual(store.content_digest(), before)
            self.assertEqual(load_entity(root).entity_config["tax_handoff"]["account_numbers"][chosen], "4100.01")
            with self.assertRaisesRegex(ValueError, "already has entries"):
                apply_starter_accounts(root, "retail")
            self.assertEqual(store.content_digest(), before)
            self.assertEqual(check_integrity(load_entity(root)).status, "ok")
            self.assertEqual(sum(e.source_id == "synthetic-retainer-1" for e in store.load_entries()), 1)
