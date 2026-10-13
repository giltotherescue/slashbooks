"""Synthetic typed Stripe client contracts; financial authority stays on server."""
from copy import deepcopy
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bookkeeping import cli, hosted
from bookkeeping.hosted_stripe import process


class StripeClientTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.child = {'request_id': 'child', 'from': '2026-01-01', 'to': '2026-01-31', 'provider_cursor': 'txn_last'}
        self.first = {'request_id': 'root', 'chain_id': 'root', 'kind': 'sync', 'from': '2026-01-01', 'to': '2026-01-31', 'state': 'completed', 'range_complete': False, 'next_request': self.child, 'next_provider_cursor': 'txn_last'}
        self.last = {**self.first, 'request_id': 'child', 'range_complete': True, 'next_request': None, 'next_provider_cursor': None}
        self.receipt = {**self.first, 'chain': {'id': 'root', 'state': 'complete', 'posted': 4, 'held': 1}}

    def run_sync(self):
        return process(self.client, 'sync', 'root', '2026-01-01', '2026-01-31')

    def test_continues_exact_server_requests_and_returns_authoritative_totals(self):
        self.client.request.side_effect = [self.first, self.last, self.receipt]
        self.assertEqual(self.run_sync(), self.receipt)
        calls = self.client.request.call_args_list
        self.assertEqual(calls[1].kwargs, {'body': self.child, 'key': 'child'})
        self.assertEqual(calls[2].args, ('/stripe/sync/root',))

    def test_interruption_propagates_and_restart_reuses_root_identity(self):
        self.client.request.side_effect = [self.first, hosted.HostedError('NETWORK_ERROR', 'unknown')]
        with self.assertRaises(hosted.HostedError): self.run_sync()
        self.assertEqual(self.client.request.call_count, 2)
        self.client.reset_mock(); self.client.request.side_effect = [self.first, self.last, self.receipt]
        self.run_sync()
        self.assertEqual(self.client.request.call_args_list[0].kwargs['key'], 'root')

    def test_malformed_and_cross_range_continuations_never_advance(self):
        for field, value in [('from', '2026-02-01'), ('request_id', '../foreign'), ('provider_cursor', 'wrong')]:
            first = deepcopy(self.first); first['next_request'][field] = value
            self.client.reset_mock(); self.client.request.side_effect = [first]
            with self.assertRaises(hosted.HostedError): self.run_sync()
            self.assertEqual(self.client.request.call_count, 1)

    def test_wrong_response_identity_is_not_success(self):
        for field, value in [('request_id', 'other'), ('kind', 'preview'), ('chain_id', 'other'), ('range_complete', 'true')]:
            self.client.request.side_effect = [{**self.first, field: value}]
            with self.assertRaises(hosted.HostedError): self.run_sync()

    def test_no_agent_authority_expansion_commands(self):
        parser = cli.build_parser()
        args = parser.parse_args(['cloud', 'stripe', 'sync', '--request-id', 'root', '--from', '2026-01-01', '--to', '2026-01-31'])
        self.assertEqual(args.stripe_command, 'sync')
        with self.assertRaises(hosted.HostedError): process(self.client, 'configure', 'root', '2026-01-01', '2026-01-31')
        self.client.request.assert_not_called()

    def test_dates_rejected_before_request(self):
        for start, end in [('20260101','2026-01-31'), ('2026-02-01','2026-01-31'), ('invalid','2026-01-31')]:
            with self.assertRaises(hosted.HostedError): process(self.client, 'sync', 'root', start, end)
        self.client.request.assert_not_called()

    def test_read_only_routes(self):
        self.client.request.return_value = {'state':'connected'}
        process(self.client, 'status'); process(self.client, 'receipt', 'root')
        self.assertEqual([c.args for c in self.client.request.call_args_list], [('/stripe',),('/stripe/sync/root',)])
        self.assertTrue(all(not c.kwargs for c in self.client.request.call_args_list))

    def test_preview_never_calls_sync_mutation(self):
        preview = {**self.last, 'request_id':'root', 'kind':'preview'}
        self.client.request.side_effect = [preview, self.receipt]
        process(self.client,'preview','root','2026-01-01','2026-01-31')
        self.assertEqual(self.client.request.call_args_list[0].args, ('/stripe/preview',))

if __name__ == '__main__': unittest.main()
