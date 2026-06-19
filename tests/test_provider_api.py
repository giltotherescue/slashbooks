from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from urllib import parse
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping import cli as cli_module  # noqa: E402
from bookkeeping.connectors.mercury import MercuryClient  # noqa: E402
from bookkeeping.connectors.stripe import StripeClient  # noqa: E402


class ConnectorCLITests(unittest.TestCase):
    def test_stripe_download_is_namespaced_under_connector(self) -> None:
        class StubStripeClient:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

            def retrieve_account(self) -> dict[str, object]:
                return {"id": "acct_stripe", "country": "US", "default_currency": "usd"}

            def list_balance_transactions(self, *, from_date: str, to_date: str) -> list[dict[str, object]]:
                return [{"id": "txn_1", "created": 1767225600, "amount": 1234}]

            def list_payouts(self, *, from_date: str, to_date: str) -> list[dict[str, object]]:
                return [{"id": "po_1", "created": 1767225600, "amount": 1200}]

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "ingestion" / "stripe" / "stripe.json"
            stdout = StringIO()
            with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_secret"}):
                with patch.object(cli_module, "StripeClient", StubStripeClient):
                    with redirect_stdout(stdout):
                        exit_code = cli_module.main(
                            [
                                "connector",
                                "stripe",
                                "download",
                                "--from",
                                "2026-01-01",
                                "--to",
                                "2026-01-31",
                                "--output",
                                str(output),
                            ]
                        )

            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"], "stripe")
            self.assertEqual(payload["summary"]["accountId"], "acct_stripe")
            self.assertEqual(payload["summary"]["balanceTransactionCount"], 1)
            self.assertIn('"output"', stdout.getvalue())

    def test_stripe_account_command_summarizes_account(self) -> None:
        class StubStripeClient:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

            def retrieve_account(self) -> dict[str, object]:
                return {
                    "id": "acct_stripe",
                    "country": "US",
                    "default_currency": "usd",
                    "charges_enabled": True,
                    "payouts_enabled": True,
                    "business_profile": {"name": "Example Co"},
                }

        stdout = StringIO()
        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_secret"}):
            with patch.object(cli_module, "StripeClient", StubStripeClient):
                with redirect_stdout(stdout):
                    exit_code = cli_module.main(["connector", "stripe", "account"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["id"], "acct_stripe")
        self.assertEqual(payload["businessName"], "Example Co")

    def test_mercury_download_is_namespaced_under_connector(self) -> None:
        class StubMercuryClient:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

            def list_accounts(self) -> list[dict[str, object]]:
                return [{"id": "acct_1", "name": "Operating"}]

            def list_credit_accounts(self) -> list[dict[str, object]]:
                return [{"id": "cred_1", "name": "Corporate Card"}]

            def list_treasury_accounts(self) -> list[dict[str, object]]:
                return [{"id": "treasury_1", "name": "Treasury"}]

            def list_account_transactions(
                self,
                account_id: str,
                *,
                from_date: str,
                to_date: str,
                date_field: str = "posted",
            ) -> list[dict[str, object]]:
                return [{"id": "txn_1", "accountId": account_id, "postedAt": "2026-01-15", "amount": "12.34"}]

            def list_transactions(
                self,
                *,
                from_date: str,
                to_date: str,
                date_field: str = "posted",
            ) -> list[dict[str, object]]:
                return [{"id": "txn_1", "postedAt": "2026-01-15", "amount": "12.34"}]

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "ingestion" / "mercury" / "mercury.json"
            stdout = StringIO()
            with patch.dict(os.environ, {"MERCURY_API_KEY": "mercury_secret"}):
                with patch.object(cli_module, "MercuryClient", StubMercuryClient):
                    with redirect_stdout(stdout):
                        exit_code = cli_module.main(
                            [
                                "connector",
                                "mercury",
                                "download",
                                "--from",
                                "2026-01-01",
                                "--to",
                                "2026-01-31",
                                "--all-accounts",
                                "--output",
                                str(output),
                            ]
                        )

            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"], "mercury")
            self.assertEqual(payload["selection"]["mode"], "selected_accounts")
            self.assertEqual(payload["summary"]["creditAccountCount"], 1)
            self.assertEqual(payload["summary"]["treasuryAccountCount"], 1)
            self.assertEqual(payload["summary"]["selectedAccountCount"], 1)
            self.assertEqual(payload["summary"]["transactionCount"], 1)
            self.assertIn('"output"', stdout.getvalue())

    def test_mercury_accounts_command_summarizes_accounts(self) -> None:
        class StubMercuryClient:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

            def list_accounts(self) -> list[dict[str, object]]:
                return [
                    {
                        "id": "acct_1",
                        "name": "Operating",
                        "type": "checking",
                        "status": "active",
                        "accountNumber": "123456789",
                    }
                ]

            def list_credit_accounts(self) -> list[dict[str, object]]:
                return [{"id": "cred_1", "name": "Corporate Card", "status": "active"}]

            def list_treasury_accounts(self) -> list[dict[str, object]]:
                return [{"id": "treasury_1", "name": "Treasury", "status": "active"}]

        stdout = StringIO()
        with patch.dict(os.environ, {"MERCURY_API_KEY": "mercury_secret"}):
            with patch.object(cli_module, "MercuryClient", StubMercuryClient):
                with redirect_stdout(stdout):
                    exit_code = cli_module.main(["connector", "mercury", "accounts"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["accounts"][0]["id"], "acct_1")
        self.assertEqual(payload["accounts"][0]["sourceType"], "operating")
        self.assertEqual(payload["accounts"][0]["accountNumberLast4"], "6789")
        self.assertEqual(payload["creditAccounts"][0]["sourceType"], "credit")
        self.assertEqual(payload["treasuryAccounts"][0]["sourceType"], "treasury")
        self.assertEqual(payload["summary"]["creditAccountCount"], 1)
        self.assertNotIn("accountNumber", payload["accounts"][0])

    def test_connector_download_missing_key_reports_env_name(self) -> None:
        stderr = StringIO()
        with patch.dict(os.environ, {}, clear=True):
            with redirect_stderr(stderr):
                exit_code = cli_module.main(
                    [
                        "connector",
                        "stripe",
                        "download",
                        "--from",
                        "2026-01-01",
                        "--to",
                        "2026-01-31",
                        "--output",
                        "/tmp/unused.json",
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("STRIPE_SECRET_KEY", stderr.getvalue())


class ProviderClientTests(unittest.TestCase):
    def test_stripe_client_uses_account_balance_transactions_and_payout_endpoints(self) -> None:
        calls: list[dict[str, object]] = []

        def transport(method: str, url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
            parsed = parse.urlparse(url)
            calls.append(
                {
                    "method": method,
                    "path": parsed.path,
                    "query": parse.parse_qs(parsed.query),
                    "headers": dict(headers),
                    "timeout": timeout,
                }
            )
            if parsed.path == "/v1/account":
                return 200, json.dumps({"id": "acct_stripe", "country": "US", "default_currency": "usd"})
            return 200, json.dumps({"data": [{"id": parsed.path.rsplit("/", 1)[-1] + "_1"}], "has_more": False})

        client = StripeClient(api_key="sk_test_secret", stripe_account="acct_connected", transport=transport)
        self.assertEqual(client.retrieve_account()["id"], "acct_stripe")
        self.assertEqual(len(client.list_balance_transactions(from_date="2026-01-01", to_date="2026-01-31")), 1)
        self.assertEqual(len(client.list_payouts(from_date="2026-01-01", to_date="2026-01-31")), 1)

        self.assertEqual(calls[0]["path"], "/v1/account")
        self.assertEqual(calls[1]["path"], "/v1/balance_transactions")
        self.assertEqual(calls[2]["path"], "/v1/payouts")
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer sk_test_secret")
        self.assertEqual(calls[0]["headers"]["Stripe-Account"], "acct_connected")
        self.assertIn("created[gte]", calls[1]["query"])
        self.assertIn("created[lte]", calls[1]["query"])

    def test_mercury_client_uses_accounts_and_transactions_endpoints(self) -> None:
        calls: list[dict[str, object]] = []

        def transport(method: str, url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
            parsed = parse.urlparse(url)
            calls.append(
                {
                    "method": method,
                    "path": parsed.path,
                    "query": parse.parse_qs(parsed.query),
                    "headers": dict(headers),
                    "timeout": timeout,
                }
            )
            if parsed.path.endswith("/accounts"):
                return 200, json.dumps({"accounts": [{"id": "acct_1"}]})
            if parsed.path.endswith("/credit"):
                return 200, json.dumps([{"id": "cred_1"}])
            if parsed.path.endswith("/treasury"):
                return 200, json.dumps({"data": [{"id": "treasury_1"}]})
            return 200, json.dumps({"transactions": [{"id": "txn_1"}]})

        client = MercuryClient(api_key="mercury_secret", transport=transport)
        self.assertEqual(len(client.list_accounts()), 1)
        self.assertEqual(len(client.list_credit_accounts()), 1)
        self.assertEqual(len(client.list_treasury_accounts()), 1)
        self.assertEqual(len(client.list_transactions(from_date="2026-01-01", to_date="2026-01-31")), 1)
        self.assertEqual(
            len(client.list_account_transactions("acct_1", from_date="2026-01-01", to_date="2026-01-31")),
            1,
        )

        self.assertEqual(calls[0]["path"], "/api/v1/accounts")
        self.assertEqual(calls[1]["path"], "/api/v1/credit")
        self.assertEqual(calls[2]["path"], "/api/v1/treasury")
        self.assertEqual(calls[3]["path"], "/api/v1/transactions")
        self.assertEqual(calls[4]["path"], "/api/v1/account/acct_1/transactions")
        self.assertEqual(calls[4]["headers"]["Authorization"], "Bearer mercury_secret")
        self.assertEqual(calls[4]["query"]["postedStart"], ["2026-01-01"])
        self.assertEqual(calls[4]["query"]["postedEnd"], ["2026-01-31"])


if __name__ == "__main__":
    unittest.main()
