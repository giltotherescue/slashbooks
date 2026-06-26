from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from urllib import parse


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bookkeeping.connectors.banksync import (  # noqa: E402
    BankSyncClient,
    BankSyncError,
    build_stats,
    collect_bank_data,
)
from bookkeeping import cli as cli_module  # noqa: E402
from bookkeeping.cli import load_dotenv, write_download  # noqa: E402


class FakeBankSyncTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
        parsed = parse.urlparse(url)
        query = parse.parse_qs(parsed.query)
        self.calls.append(
            {
                "method": method,
                "path": parsed.path,
                "query": query,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        data = self.route(parsed.path)
        return 200, json.dumps({"success": True, "data": data})

    def route(self, path: str) -> object:
        if path == "/v1/banks":
            return [
                {
                    "id": "bank_mercury",
                    "name": "Mercury",
                    "source": "plaid",
                    "type": "bank",
                    "institutionId": "ins_116794",
                    "createdAt": "2026-06-12T16:49:37.428Z",
                    "updatedAt": "2026-06-12T16:49:37.428Z",
                },
                {
                    "id": "bank_other",
                    "name": "Other Bank",
                    "source": "plaid",
                    "type": "bank",
                },
            ]

        if path == "/v1/banks/bank_mercury/accounts":
            return [
                {
                    "id": "acct_ops",
                    "bankId": "bank_mercury",
                    "accountName": "Operating Expenses",
                    "accountType": "checking",
                    "accountNumber": "123456789",
                    "balance": 125.5,
                    "availableBalance": 100.25,
                    "currency": "USD",
                },
                {
                    "id": "acct_credit",
                    "bankId": "bank_mercury",
                    "accountName": "Mercury Credit",
                    "accountType": "credit_card",
                    "accountNumber": "0000",
                    "balance": 0,
                    "availableBalance": 3600,
                    "currency": "USD",
                },
            ]
        if path == "/v1/banks/bank_mercury/accounts/acct_ops/transactions":
            return [
                {
                    "id": "txn_1",
                    "date": "2026-01-02T14:27:13.000Z",
                    "description": "Client payment",
                    "amount": 100,
                    "creditAmount": 100,
                    "debitAmount": 0,
                    "currency": "USD",
                    "category": "Transfer",
                    "type": "Online",
                    "pending": False,
                    "accountId": "acct_ops",
                    "accountNumber": "123456789",
                    "bankId": "bank_mercury",
                },
                {
                    "id": "txn_2",
                    "date": "2026-01-03T14:27:13.000Z",
                    "description": "Software",
                    "amount": -25.4,
                    "creditAmount": 0,
                    "debitAmount": 25.4,
                    "currency": "USD",
                    "category": "Software",
                    "type": "Online",
                    "pending": True,
                    "accountId": "acct_ops",
                    "accountNumber": "123456789",
                    "bankId": "bank_mercury",
                },
            ]
        if path == "/v1/banks/bank_mercury/accounts/acct_credit/transactions":
            return [
                {
                    "id": "txn_3",
                    "date": "2026-01-04T14:27:13.000Z",
                    "description": "Card payment",
                    "amount": -100,
                    "creditAmount": 0,
                    "debitAmount": 100,
                    "currency": "USD",
                    "category": "Transfer",
                    "type": "Other",
                    "pending": False,
                    "accountId": "acct_credit",
                    "accountNumber": "0000",
                    "bankId": "bank_mercury",
                }
            ]
        raise AssertionError(f"Unexpected path: {path}")


class TestDotenvLoading(unittest.TestCase):
    def test_load_dotenv_loads_simple_values_without_overriding_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            dotenv.write_text(
                "\n".join(
                    [
                        "# local credentials",
                        "BANKSYNC_API_KEY='from-file'",
                        'OTHER_KEY="quoted"',
                        "export THIRD_KEY=plain",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"BANKSYNC_API_KEY": "from-env"}, clear=False):
                os.environ.pop("OTHER_KEY", None)
                os.environ.pop("THIRD_KEY", None)
                load_dotenv(dotenv)
                self.assertEqual(os.environ["BANKSYNC_API_KEY"], "from-env")
                self.assertEqual(os.environ["OTHER_KEY"], "quoted")
                self.assertEqual(os.environ["THIRD_KEY"], "plain")

    def test_load_dotenv_ignores_invalid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            dotenv.write_text(
                "\n".join(
                    [
                        "not a key",
                        "1BAD=value",
                        "BAD-NAME=value",
                        "GOOD_NAME=value",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                load_dotenv(dotenv)
                self.assertNotIn("1BAD", os.environ)
                self.assertNotIn("BAD-NAME", os.environ)
                self.assertEqual(os.environ["GOOD_NAME"], "value")


# ---------------------------------------------------------------------------
# Multi-page pagination transport — 3 pages, 250 transactions total
# ---------------------------------------------------------------------------

def _make_txn(idx: int, account_id: str = "acct_ops") -> dict[str, object]:
    """Return a minimal transaction dict with a predictable ID and date."""
    return {
        "id": f"txn_page_{idx:04d}",
        "date": f"2026-01-{(idx % 28) + 1:02d}T00:00:00.000Z",
        "description": f"Transaction {idx}",
        "amount": -1.00,
        "creditAmount": 0,
        "debitAmount": 1.00,
        "currency": "USD",
        "type": "Online",
        "pending": False,
        "accountId": account_id,
        "bankId": "bank_paged",
    }


class MultiPageTransport:
    """Fake transport routing paginated transaction requests for a single account.

    Pages:
      page 1 (no cursor arg)     → 100 txns + nextCursor="cursor_p2"
      page 2 (cursor=cursor_p2)  → 100 txns + nextCursor="cursor_p3"
      page 3 (cursor=cursor_p3)  →  50 txns + no cursor (exhausted)
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        # Build pages: indices 0-99, 100-199, 200-249
        self._pages = [
            ([_make_txn(i) for i in range(100)], "cursor_p2"),
            ([_make_txn(i) for i in range(100, 200)], "cursor_p3"),
            ([_make_txn(i) for i in range(200, 250)], None),
        ]

    def __call__(self, method: str, url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
        parsed = parse.urlparse(url)
        query = dict(parse.parse_qsl(parsed.query))
        self.calls.append({"method": method, "path": parsed.path, "query": query})

        if parsed.path == "/v1/banks/bank_paged/accounts":
            return 200, json.dumps({"success": True, "data": [{"id": "acct_ops", "bankId": "bank_paged",
                                                                "accountName": "Ops", "accountType": "checking",
                                                                "currency": "USD"}]})
        if parsed.path == "/v1/banks/bank_paged/accounts/acct_ops/transactions":
            cursor = query.get("cursor")
            if cursor is None:
                page_idx = 0
            elif cursor == "cursor_p2":
                page_idx = 1
            elif cursor == "cursor_p3":
                page_idx = 2
            else:
                return 500, json.dumps({"success": False, "error": f"Unknown cursor: {cursor}"})
            txns, next_cursor = self._pages[page_idx]
            envelope: dict[str, object] = {"success": True, "data": txns}
            if next_cursor:
                envelope["nextCursor"] = next_cursor
            return 200, json.dumps(envelope)

        return 404, json.dumps({"success": False, "error": "not found"})


class EmptyPageWithCursorTransport:
    """Page 1 returns empty data but a cursor; page 2 returns data and no cursor."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
        parsed = parse.urlparse(url)
        query = dict(parse.parse_qsl(parsed.query))
        self.calls.append({"path": parsed.path, "query": query})

        if parsed.path == "/v1/banks/bank_e/accounts/acct_e/transactions":
            cursor = query.get("cursor")
            if cursor is None:
                return 200, json.dumps({"success": True, "data": [], "nextCursor": "cursor_e2"})
            else:
                return 200, json.dumps({"success": True, "data": [_make_txn(0)]})
        return 404, json.dumps({"success": False, "error": "not found"})


class MidPaginationErrorTransport:
    """First page succeeds; second page returns HTTP 500."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
        parsed = parse.urlparse(url)
        query = dict(parse.parse_qsl(parsed.query))
        self.calls.append({"path": parsed.path, "query": query})

        if parsed.path == "/v1/banks/bank_err/accounts/acct_err/transactions":
            cursor = query.get("cursor")
            if cursor is None:
                return 200, json.dumps({"success": True, "data": [_make_txn(0)], "nextCursor": "cursor_err2"})
            else:
                return 500, json.dumps({"success": False, "error": "Internal server error"})
        return 404, json.dumps({"success": False, "error": "not found"})


class BankSyncTests(unittest.TestCase):
    def test_client_sends_api_key_header_and_date_filters(self) -> None:
        transport = FakeBankSyncTransport()
        client = BankSyncClient(api_key="test_key", transport=transport)

        client.list_transactions(
            "bank_mercury",
            "acct_ops",
            from_date="2026-01-01",
            to_date="2026-01-31",
        )

        call = transport.calls[0]
        self.assertEqual(call["headers"]["X-API-Key"], "test_key")
        self.assertEqual(call["query"], {"from": ["2026-01-01"], "to": ["2026-01-31"]})

    def test_collect_bank_data_and_stats_sanitize_accounts(self) -> None:
        transport = FakeBankSyncTransport()
        client = BankSyncClient(api_key="test_key", transport=transport)

        collected = collect_bank_data(
            client,
            bank_name="Mercury",
            from_date="2026-01-01",
            to_date="2026-01-31",
        )
        stats = build_stats(collected)

        self.assertEqual(stats["connectedBankCount"], 2)
        self.assertEqual(stats["matchedBankCount"], 1)
        self.assertEqual(stats["matchedAccountCount"], 2)
        self.assertEqual(stats["transactionSummary"]["count"], 3)
        self.assertEqual(stats["transactionSummary"]["posted"], 2)
        self.assertEqual(stats["transactionSummary"]["pending"], 1)
        self.assertEqual(stats["transactionSummary"]["creditAmountTotal"], "100.00")
        self.assertEqual(stats["transactionSummary"]["debitAmountTotal"], "125.40")
        self.assertEqual(stats["transactionSummary"]["netAmountTotal"], "-25.40")
        self.assertEqual(
            stats["banks"][0]["accounts"][0]["account"]["accountNumberLast4"],
            "6789",
        )
        self.assertNotIn("accountNumber", stats["banks"][0]["accounts"][0]["account"])
        self.assertEqual(collected["banks"][0]["accounts"][0]["transactions"][0]["accountNumberLast4"], "6789")
        self.assertEqual(collected["banks"][0]["accounts"][0]["transactions"][0]["accountType"], "checking")
        self.assertEqual(collected["banks"][0]["accounts"][1]["transactions"][0]["accountType"], "credit_card")

    def test_write_download_refuses_to_overwrite_without_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "banksync.json"
            write_download(path, {"ok": True}, overwrite=False)

            with self.assertRaises(Exception):
                write_download(path, {"ok": False}, overwrite=False)

            write_download(path, {"ok": False}, overwrite=True)
            self.assertEqual(json.loads(path.read_text())["ok"], False)

    def test_accounts_cli_outputs_normalized_accounts(self) -> None:
        class StubClient:
            def __init__(self, **kwargs: object) -> None:
                pass

            def list_accounts(self, bank_id: str) -> list[dict[str, object]]:
                return [
                    {
                        "id": "acct_ops",
                        "bankId": bank_id,
                        "accountName": "Operating Expenses",
                        "accountType": "checking",
                        "accountNumber": "123456789",
                        "balance": 125.5,
                        "availableBalance": 100.25,
                        "currency": "USD",
                    }
                ]

        stdout = StringIO()
        with patch.dict(os.environ, {"BANKSYNC_API_KEY": "test_key"}):
            with patch.object(cli_module, "BankSyncClient", StubClient):
                with redirect_stdout(stdout):
                    exit_code = cli_module.main(["connector", "banksync", "accounts", "--bank-id", "bank_mercury"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["accountNumberLast4"], "6789")
        self.assertNotIn("accountNumber", payload[0])


# ---------------------------------------------------------------------------
# Pagination tests
# ---------------------------------------------------------------------------

class PaginationTests(unittest.TestCase):
    def test_three_pages_returns_all_250_transactions_in_order(self) -> None:
        """Happy path: 3-page transport yields all 250 txns exactly once, in order."""
        transport = MultiPageTransport()
        client = BankSyncClient(api_key="test_key", transport=transport)

        txns = client.list_transactions("bank_paged", "acct_ops")

        self.assertEqual(len(txns), 250)
        ids = [t["id"] for t in txns]
        expected_ids = [f"txn_page_{i:04d}" for i in range(250)]
        self.assertEqual(ids, expected_ids)

    def test_three_pages_passes_cursors_correctly(self) -> None:
        """Each subsequent request carries the cursor from the previous response envelope."""
        transport = MultiPageTransport()
        client = BankSyncClient(api_key="test_key", transport=transport)

        client.list_transactions("bank_paged", "acct_ops")

        # Filter to only transaction calls (not accounts)
        txn_calls = [c for c in transport.calls if "transactions" in c["path"]]
        self.assertEqual(len(txn_calls), 3)
        # First call: no cursor
        self.assertNotIn("cursor", txn_calls[0]["query"])
        # Second call: cursor from page 1
        self.assertEqual(txn_calls[1]["query"].get("cursor"), "cursor_p2")
        # Third call: cursor from page 2
        self.assertEqual(txn_calls[2]["query"].get("cursor"), "cursor_p3")

    def test_empty_page_with_cursor_continues_to_next_page(self) -> None:
        """An empty data array with a cursor present does not stop pagination."""
        transport = EmptyPageWithCursorTransport()
        client = BankSyncClient(api_key="test_key", transport=transport)

        txns = client.list_transactions("bank_e", "acct_e")

        self.assertEqual(len(txns), 1)
        self.assertEqual(len(transport.calls), 2)

    def test_single_page_response_unchanged(self) -> None:
        """A response with no cursor returns the same result as before pagination."""
        transport = FakeBankSyncTransport()
        client = BankSyncClient(api_key="test_key", transport=transport)

        txns = client.list_transactions("bank_mercury", "acct_ops")

        self.assertEqual(len(txns), 2)
        self.assertEqual(txns[0]["id"], "txn_1")
        self.assertEqual(txns[1]["id"], "txn_2")

    def test_mid_pagination_http_500_raises_banksync_error(self) -> None:
        """A server error mid-pagination raises BankSyncError immediately (no partial silent success)."""
        transport = MidPaginationErrorTransport()
        client = BankSyncClient(api_key="test_key", transport=transport)

        with self.assertRaises(BankSyncError) as ctx:
            client.list_transactions("bank_err", "acct_err")

        self.assertIsNotNone(ctx.exception.status)
        self.assertEqual(ctx.exception.status, 500)
        # Exactly 2 transport calls were made (first succeeded, second failed)
        self.assertEqual(len(transport.calls), 2)

    def test_collect_bank_data_benefits_from_pagination(self) -> None:
        """collect_bank_data automatically gets all pages via list_transactions."""
        transport = MultiPageTransport()
        client = BankSyncClient(api_key="test_key", transport=transport)

        # We need list_banks to work — stub it via a wrapper
        class BanksWrapper:
            def __init__(self, inner: BankSyncClient) -> None:
                self._inner = inner

            def list_banks(self, *, scope: str | None = None) -> list[dict[str, object]]:
                return [{"id": "bank_paged", "name": "Paged Bank", "source": "plaid", "type": "bank"}]

            def list_accounts(self, bank_id: str) -> list[dict[str, object]]:
                return self._inner.list_accounts(bank_id)

            def list_transactions(self, bank_id: str, account_id: str, **kwargs: object) -> list[dict[str, object]]:
                return self._inner.list_transactions(bank_id, account_id, **kwargs)

        wrapper = BanksWrapper(client)
        collected = collect_bank_data(
            wrapper,  # type: ignore[arg-type]
            bank_name="Paged Bank",
            from_date="2026-01-01",
            to_date="2026-01-31",
        )
        total = sum(
            len(acct["transactions"])
            for bank in collected["banks"]
            for acct in bank["accounts"]
        )
        self.assertEqual(total, 250)


# ---------------------------------------------------------------------------
# Validate subcommand tests
# ---------------------------------------------------------------------------

def _make_validate_transport(
    *,
    stable: bool = True,
    page_count: int = 1,
) -> "ValidateTransport":
    return ValidateTransport(stable=stable, page_count=page_count)


class ValidateTransport:
    """Transport for validate subcommand tests.

    When stable=True, both fetches return the same transaction IDs.
    When stable=False, the second fetch returns a different set (IDs mutated).
    page_count controls whether responses include pagination cursors (>1).
    """

    def __init__(self, *, stable: bool, page_count: int) -> None:
        self._stable = stable
        self._page_count = page_count
        self._fetch_counts: dict[str, int] = {}
        self.calls: list[dict[str, object]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
        parsed = parse.urlparse(url)
        query = dict(parse.parse_qsl(parsed.query))
        self.calls.append({"path": parsed.path, "query": query})

        if parsed.path == "/v1/banks":
            return 200, json.dumps({"success": True, "data": [
                {"id": "bank_v", "name": "ValidateBank", "source": "plaid", "type": "bank"}
            ]})

        if parsed.path == "/v1/banks/bank_v/accounts":
            return 200, json.dumps({"success": True, "data": [
                {"id": "acct_v", "bankId": "bank_v", "accountName": "Main", "accountType": "checking",
                 "currency": "USD", "balance": 1000, "availableBalance": 1000, "accountNumber": "9999"}
            ]})

        if parsed.path == "/v1/banks/bank_v/accounts/acct_v/transactions":
            key = parsed.path
            self._fetch_counts[key] = self._fetch_counts.get(key, 0) + 1
            fetch_num = self._fetch_counts[key]

            cursor = query.get("cursor")
            if self._page_count == 1 or cursor == "cursor_v2":
                # Last/only page — no cursor
                if not self._stable and fetch_num > 1:
                    # Second fetch returns different IDs (instability)
                    txns = [_make_txn(i, "acct_v") for i in range(5, 10)]
                else:
                    txns = [_make_txn(i, "acct_v") for i in range(5)]
                return 200, json.dumps({"success": True, "data": txns})
            else:
                # First page with cursor to second
                txns = [_make_txn(i, "acct_v") for i in range(5)]
                return 200, json.dumps({"success": True, "data": txns, "nextCursor": "cursor_v2"})

        return 404, json.dumps({"success": False, "error": "not found"})


class ValidateSubcommandTests(unittest.TestCase):
    def _run_validate(self, transport: ValidateTransport, extra_args: list[str] | None = None) -> dict[str, object]:
        stdout = StringIO()
        args = [
            "connector", "banksync", "validate",
            "--bank-name", "ValidateBank",
            "--from", "2026-01-01",
            "--to", "2026-01-31",
        ]
        if extra_args:
            args.extend(extra_args)
        with patch.dict(os.environ, {"BANKSYNC_API_KEY": "test_key"}):
            with patch.object(cli_module, "BankSyncClient", lambda **kwargs: BankSyncClient(api_key="test_key", transport=transport)):
                with redirect_stdout(stdout):
                    exit_code = cli_module.main(args)
        self.assertEqual(exit_code, 0)
        return json.loads(stdout.getvalue())  # type: ignore[return-value]

    def test_validate_stable_ids_reports_stable(self) -> None:
        """When both fetches return identical transaction IDs, report stability=stable."""
        transport = _make_validate_transport(stable=True)
        report = self._run_validate(transport)

        self.assertIn("id_stability", report)
        self.assertEqual(report["id_stability"]["stability"], "stable")
        self.assertEqual(report["id_stability"]["run1_count"], 5)
        self.assertEqual(report["id_stability"]["run2_count"], 5)
        self.assertEqual(report["id_stability"]["ids_only_in_run1"], [])
        self.assertEqual(report["id_stability"]["ids_only_in_run2"], [])

    def test_validate_unstable_ids_flags_instability(self) -> None:
        """When second fetch returns different IDs, report stability=unstable with diffs."""
        transport = _make_validate_transport(stable=False)
        report = self._run_validate(transport)

        self.assertIn("id_stability", report)
        self.assertEqual(report["id_stability"]["stability"], "unstable")
        # Run1 has IDs 0-4, run2 has IDs 5-9 — all are unique to one run
        self.assertGreater(len(report["id_stability"]["ids_only_in_run1"]), 0)
        self.assertGreater(len(report["id_stability"]["ids_only_in_run2"]), 0)

    def test_validate_report_contains_required_sections(self) -> None:
        """Validate report always contains id_stability, historical_depth, pending_posted, per_account."""
        transport = _make_validate_transport(stable=True)
        report = self._run_validate(transport)

        for section in ("id_stability", "historical_depth", "pending_posted", "per_account"):
            self.assertIn(section, report, f"Missing section: {section}")

    def test_validate_historical_depth_section(self) -> None:
        """historical_depth reports the earliest returned transaction date."""
        transport = _make_validate_transport(stable=True)
        report = self._run_validate(transport)

        depth = report["historical_depth"]
        self.assertIn("requested_from", depth)
        self.assertIn("earliest_returned_date", depth)
        self.assertEqual(depth["requested_from"], "2026-01-01")

    def test_validate_pending_posted_section(self) -> None:
        """pending_posted section contains counts and pending field list."""
        transport = _make_validate_transport(stable=True)
        report = self._run_validate(transport)

        pp = report["pending_posted"]
        self.assertIn("total_count", pp)
        self.assertIn("posted_count", pp)
        self.assertIn("pending_count", pp)
        self.assertIn("pending_fields", pp)

    def test_validate_per_account_section(self) -> None:
        """per_account section lists count per account with suspected truncation flag."""
        transport = _make_validate_transport(stable=True)
        report = self._run_validate(transport)

        per_acct = report["per_account"]
        self.assertIsInstance(per_acct, list)
        self.assertGreater(len(per_acct), 0)
        entry = per_acct[0]
        self.assertIn("account_id", entry)
        self.assertIn("transaction_count", entry)
        self.assertIn("suspected_truncation", entry)

    def test_validate_writes_output_file_when_specified(self) -> None:
        """--output writes the JSON report to the given path."""
        transport = _make_validate_transport(stable=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "report.json"
            self._run_validate(transport, extra_args=["--output", str(out_path)])
            self.assertTrue(out_path.exists())
            written = json.loads(out_path.read_text())
            self.assertIn("id_stability", written)

    def test_validate_does_not_print_api_key(self) -> None:
        """API key must never appear in validate output."""
        transport = _make_validate_transport(stable=True)
        stdout = StringIO()
        fake_token = "redacted-test-token"
        with patch.dict(os.environ, {"BANKSYNC_API_KEY": fake_token}):
            with patch.object(cli_module, "BankSyncClient", lambda **kwargs: BankSyncClient(api_key=fake_token, transport=transport)):
                with redirect_stdout(stdout):
                    cli_module.main([
                        "connector", "banksync", "validate",
                        "--bank-name", "ValidateBank",
                        "--from", "2026-01-01",
                        "--to", "2026-01-31",
                    ])
        self.assertNotIn(fake_token, stdout.getvalue())

    def test_validate_pagination_fetches_all_pages_both_runs(self) -> None:
        """With a multi-page transport, validate fetches all pages on both runs."""
        transport = _make_validate_transport(stable=True, page_count=2)
        report = self._run_validate(transport)
        # 5 txns per page × 2 pages = 10 total per run; both runs should see the same 10
        self.assertEqual(report["id_stability"]["run1_count"], 10)
        self.assertEqual(report["id_stability"]["run2_count"], 10)
        self.assertEqual(report["id_stability"]["stability"], "stable")


class FlakyAccountsTransport(FakeBankSyncTransport):
    """Returns empty accounts (or 500s) for the first N accounts calls, then real data."""

    def __init__(self, *, empty_calls: int = 0, fail_calls: int = 0) -> None:
        super().__init__()
        self.empty_remaining = empty_calls
        self.fail_remaining = fail_calls

    def __call__(self, method: str, url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
        parsed = parse.urlparse(url)
        if parsed.path.endswith("/accounts"):
            if self.fail_remaining > 0:
                self.fail_remaining -= 1
                return 500, json.dumps({"success": False, "error": "Internal server error"})
            if self.empty_remaining > 0:
                self.empty_remaining -= 1
                return 200, json.dumps({"success": True, "data": []})
        return super().__call__(method, url, headers, timeout)


class TransientRetryTests(unittest.TestCase):
    def test_empty_accounts_for_connected_bank_retried_with_backoff(self) -> None:
        transport = FlakyAccountsTransport(empty_calls=2)
        client = BankSyncClient(api_key="key", transport=transport)
        delays: list[float] = []
        collected = collect_bank_data(
            client,
            bank_name="Mercury",
            from_date="2026-01-01",
            to_date="2026-01-31",
            sleeper=delays.append,
        )
        accounts = collected["banks"][0]["accounts"]
        self.assertGreater(len(accounts), 0, "retry should recover the real accounts")
        self.assertGreaterEqual(len(delays), 2, "backoff sleeper should have been invoked")

    def test_transient_500_on_accounts_retried(self) -> None:
        transport = FlakyAccountsTransport(fail_calls=1)
        client = BankSyncClient(api_key="key", transport=transport)
        collected = collect_bank_data(
            client,
            bank_name="Mercury",
            from_date="2026-01-01",
            to_date="2026-01-31",
            sleeper=lambda _delay: None,
        )
        self.assertGreater(len(collected["banks"][0]["accounts"]), 0)

    def test_persistent_500_still_raises(self) -> None:
        transport = FlakyAccountsTransport(fail_calls=99)
        client = BankSyncClient(api_key="key", transport=transport)
        with self.assertRaises(BankSyncError):
            collect_bank_data(
                client,
                bank_name="Mercury",
                from_date="2026-01-01",
                to_date="2026-01-31",
                sleeper=lambda _delay: None,
            )


if __name__ == "__main__":
    unittest.main()
