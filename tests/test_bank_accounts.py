"""Provider-neutral bank account identity: rich mappings and match-account."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookkeeping.bank_accounts import propose_account_match  # noqa: E402
from bookkeeping.cli import main  # noqa: E402
from bookkeeping.entity import (  # noqa: E402
    bank_mapping_account, flat_bank_account_mappings, init_entity, load_entity, map_bank_account, add_account,
)
from bookkeeping.ledger.importer import _ledger_account_for_txn  # noqa: E402

CHECKING = "Assets:Bank:Operating-Checking"
SAVINGS = "Assets:Bank:Reserve-Savings"


def _entity(tmp: str) -> Path:
    root = Path(tmp).resolve() / "company"
    init_entity(root, name="Harbor Studio", business_type="consulting", legal_structure="llc")
    for account in (CHECKING, SAVINGS):
        add_account(root, account, open_date="2025-01-01")
    return root


def _read_mappings(root: Path) -> dict:
    return json.loads((root / "entity.json").read_text(encoding="utf-8"))["bank_account_mappings"]


class RichMappingTests(unittest.TestCase):
    def test_flat_mapping_is_written_and_read_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _entity(tmp)
            report = map_bank_account(root, "acct_1", CHECKING)
            self.assertEqual(report["status"], "created")
            self.assertEqual(_read_mappings(root), {"acct_1": CHECKING})
            self.assertEqual(flat_bank_account_mappings(load_entity(root).entity_config), {"acct_1": CHECKING})

    def test_identity_fields_write_the_rich_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _entity(tmp)
            map_bank_account(root, "acct_1", CHECKING, provider="banksync", institution_id="ins_1",
                             institution_name="First Bank", last4="0021", account_type="Checking",
                             currency="usd", cutover_date="2026-02-01")
            self.assertEqual(_read_mappings(root)["acct_1"], {
                "ledger_account": CHECKING, "provider": "banksync", "provider_account_id": "acct_1",
                "institution_id": "ins_1", "institution_name": "First Bank", "last4": "0021",
                "account_type": "checking", "currency": "USD", "cutover_date": "2026-02-01"})

    def test_remapping_a_rich_entry_without_fields_keeps_its_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _entity(tmp)
            map_bank_account(root, "acct_1", CHECKING, provider="banksync", last4="0021")
            report = map_bank_account(root, "acct_1", SAVINGS)
            self.assertEqual(report["status"], "updated")
            entry = _read_mappings(root)["acct_1"]
            self.assertEqual((entry["ledger_account"], entry["provider"], entry["last4"]),
                             (SAVINGS, "banksync", "0021"))

    def test_bad_identity_values_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _entity(tmp)
            for field, value in (("last4", "12"), ("currency", "DOLLAR"), ("cutover_date", "02/01/2026")):
                with self.assertRaises(ValueError):
                    map_bank_account(root, "acct_1", CHECKING, **{field: value})
            self.assertEqual(_read_mappings(root), {})

    def test_importer_resolves_both_forms_by_account_id(self) -> None:
        mappings = {"flat": CHECKING, "rich": {"ledger_account": SAVINGS, "last4": "9999"}}
        self.assertEqual(_ledger_account_for_txn({"accountId": "flat"}, mappings), CHECKING)
        self.assertEqual(_ledger_account_for_txn({"accountId": "rich"}, mappings), SAVINGS)
        self.assertEqual(bank_mapping_account({"ledger_account": ""}), None)

    def test_cli_map_accepts_identity_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _entity(tmp)
            code = main(["entity", "bank-account-map", str(root), "--feed-account-id", "acct_1",
                         "--account", CHECKING, "--provider", "plaid", "--last4", "0021",
                         "--cutover-date", "2026-03-01"])
            self.assertEqual(code, 0)
            self.assertEqual(_read_mappings(root)["acct_1"]["cutover_date"], "2026-03-01")


class MatchAccountTests(unittest.TestCase):
    def _mapped(self, tmp: str) -> Path:
        root = _entity(tmp)
        map_bank_account(root, "bs_chk", CHECKING, provider="banksync", institution_id="ins_chase",
                         institution_name="Chase", last4="0021", account_type="checking", currency="USD")
        map_bank_account(root, "bs_sav", SAVINGS, provider="banksync", institution_id="ins_chase",
                         institution_name="Chase", last4="0099", account_type="savings", currency="USD")
        map_bank_account(root, "legacy", "Assets:Bank:Operating-Checking")  # flat: no identity
        return root

    def _new(self, **overrides: str) -> dict:
        base = {"provider": "plaid", "provider_account_id": "plaid_1", "institution_id": "ins_3",
                "institution_name": "JPMorgan Chase", "last4": "0021", "account_type": "checking",
                "currency": "USD"}
        base.update(overrides)
        return base

    def test_same_name_last4_type_currency_proposes_the_ledger_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._mapped(tmp)
            result = propose_account_match(load_entity(root), self._new(institution_name="chase"))
            self.assertEqual(result["status"], "proposal")
            self.assertEqual(result["ledger_account"], CHECKING)
            self.assertEqual(result["candidates"][0]["existing_provider_account_ids"], ["bs_chk"])
            self.assertIn("matched_on", result["candidates"][0])
            self.assertIn("bank-account-map", result["confirm_command"])
            self.assertIn('--feed-account-id "plaid_1"', result["confirm_command"])

    def test_same_institution_id_matches_when_names_differ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._mapped(tmp)
            result = propose_account_match(load_entity(root), self._new(institution_id="ins_chase"))
            self.assertEqual((result["status"], result["ledger_account"]), ("proposal", CHECKING))

    def test_a_proposal_never_changes_the_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._mapped(tmp)
            before = (root / "entity.json").read_bytes()
            propose_account_match(load_entity(root), self._new(institution_name="Chase"))
            self.assertEqual((root / "entity.json").read_bytes(), before)

    def test_any_difference_gives_no_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._mapped(tmp)
            entity = load_entity(root)
            for change in ({"last4": "7777"}, {"account_type": "savings", "last4": "0021"},
                           {"currency": "EUR"}, {"institution_name": "Other Bank", "institution_id": "x"}):
                result = propose_account_match(entity, self._new(**{"institution_name": "Chase", **change}))
                self.assertEqual(result["status"], "no_match", change)
                self.assertIsNone(result["ledger_account"])

    def test_missing_identity_field_gives_no_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._mapped(tmp)
            result = propose_account_match(load_entity(root), self._new(institution_name="Chase", last4=""))
            self.assertEqual(result["status"], "no_match")
            self.assertIn("last4", result["reason"])

    def test_two_ledger_accounts_that_match_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._mapped(tmp)
            map_bank_account(root, "bs_other", SAVINGS, provider="banksync", institution_name="Chase",
                             last4="0021", account_type="checking", currency="USD")
            result = propose_account_match(load_entity(root), self._new(institution_name="Chase"))
            self.assertEqual(result["status"], "ambiguous")
            self.assertEqual(len(result["candidates"]), 2)
            self.assertIsNone(result["ledger_account"])

    def test_an_already_mapped_provider_id_is_reported_not_proposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._mapped(tmp)
            result = propose_account_match(load_entity(root), self._new(provider_account_id="bs_chk"))
            self.assertEqual((result["status"], result["ledger_account"]), ("already_mapped", CHECKING))

    def test_cli_prints_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._mapped(tmp)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["bank", "match-account", "--entity", str(root), "--provider", "plaid",
                             "--provider-account-id", "plaid_1", "--institution-name", "Chase",
                             "--last4", "0021", "--account-type", "checking", "--currency", "USD", "--json"])
            self.assertEqual(code, 0)
            document = json.loads(out.getvalue())
            self.assertEqual((document["kind"], document["status"]), ("bank_account_match", "proposal"))
            self.assertEqual(document["ledger_account"], CHECKING)


class RichMappingConsumersTests(unittest.TestCase):
    def test_balance_check_finds_the_ledger_account_of_a_rich_mapping(self) -> None:
        from bookkeeping.tieout import balance_check
        with tempfile.TemporaryDirectory() as tmp:
            root = _entity(tmp)
            map_bank_account(root, "bs_chk", CHECKING, provider="banksync", last4="0021")
            rows = balance_check(root, [{"account_id": "bs_chk", "balance": "0.00",
                                         "as_of": "2026-01-31", "data_through": "2026-01-31"}])["accounts"]
            self.assertEqual(rows[0]["ledger_account"], CHECKING)

    def test_accrual_cash_account_set_includes_a_rich_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _entity(tmp)
            map_bank_account(root, "bs_chk", CHECKING, provider="banksync", last4="0021")
            map_bank_account(root, "legacy", SAVINGS)
            self.assertEqual(set(flat_bank_account_mappings(load_entity(root).entity_config).values()),
                             {CHECKING, SAVINGS})


class RemoteContractTests(unittest.TestCase):
    def test_match_account_is_a_read_and_bank_account_map_is_a_write(self) -> None:
        from bookkeeping.remote_contract import command_spec
        read = command_spec(["bank", "match-account", "--entity", "/tmp/x", "--last4", "0021", "--json"])
        self.assertFalse(read["mutates"])
        write = command_spec(["entity", "bank-account-map", "/tmp/x", "--feed-account-id", "a", "--account", "b"])
        self.assertTrue(write["mutates"])


class BankSyncInstitutionTests(unittest.TestCase):
    def test_normalize_account_keeps_the_institution_when_sent(self) -> None:
        from bookkeeping.connectors.banksync import normalize_account
        base = {"id": "a1", "accountNumber": "000012340021", "accountType": "checking", "currency": "USD"}
        plain = normalize_account(base)
        self.assertNotIn("institutionId", plain)
        self.assertNotIn("institutionName", plain)
        sent = normalize_account({**base, "institutionId": "ins_1", "institutionName": "First Bank"})
        self.assertEqual((sent["institutionId"], sent["institutionName"]), ("ins_1", "First Bank"))
        nested = normalize_account({**base, "institution": {"id": "ins_2", "name": "Second Bank"}})
        self.assertEqual((nested["institutionId"], nested["institutionName"]), ("ins_2", "Second Bank"))
        self.assertEqual({k: v for k, v in sent.items() if k not in ("institutionId", "institutionName")}, plain)


if __name__ == "__main__":
    unittest.main()
