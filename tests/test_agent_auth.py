from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bookkeeping import agent_auth, hosted, remote


class AgentAuthTests(unittest.TestCase):
    def test_sign_in_transport_identifies_slashbooks_without_secrets_in_headers(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"interval":5}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(agent_auth.request, "build_opener", return_value=opener):
            self.assertEqual(agent_auth._post("https://books.example.com/api/v1", "/oauth/token",
                                             {"refresh_token": "synthetic-secret"}), {"interval": 5})
        req = opener.open.call_args.args[0]
        self.assertEqual(req.get_header("User-agent"), f"slashbooks/{agent_auth.__version__}")
        self.assertNotIn("synthetic-secret", str(req.headers))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.directory = self.home / "company"
        self.args = SimpleNamespace(endpoint="https://books.example.com", company="company",
                                    entity=str(self.directory), allow_localhost=False, reauthorize=False)
        self.start = {"device_code": "synthetic-device-secret", "user_code": "ABCDE-FG234",
                      "verification_uri_complete": "https://books.example.com/connect-agent?user_code=ABCDE-FG234",
                      "interval": 5, "expires_in": 600}
        self.tokens = {"access_token": "sba_synthetic_access_never_print", "refresh_token": "sbr_synthetic_refresh_never_print_12345",
                       "token_type": "Bearer", "company_id": "company", "expires_in": 3600}

    def login(self, responses):
        output = StringIO()
        with patch.object(agent_auth, "_post", side_effect=responses), patch.object(agent_auth.time, "sleep"), \
                patch.object(Path, "home", return_value=self.home), \
                patch.object(hosted.HostedClient, "request", return_value={"id": "company", "name": "Synthetic Company"}), redirect_stdout(output):
            self.assertEqual(agent_auth.login(self.args), 0)
        return output.getvalue()

    def test_login_verifies_and_saves_private_binding_without_secrets_in_output(self):
        output = self.login([self.start, {"error": "authorization_pending"}, self.tokens])
        for secret in [self.start["device_code"], self.tokens["access_token"], self.tokens["refresh_token"]]:
            self.assertNotIn(secret, output)
        self.assertIn("confirmation_code", output)
        config_path = self.directory / remote.CONFIG_NAME
        config = json.loads(config_path.read_text())
        self.assertNotIn("token", config)
        credential = Path(config["credential_file"])
        self.assertFalse(credential.is_relative_to(self.directory))
        for path in [config_path, credential]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.directory / "ledger.sqlite").exists())

    def test_denied_grant_does_not_bind_or_print_credentials(self):
        with patch.object(agent_auth, "_post", side_effect=[self.start, {"error": "access_denied"}]), \
                patch.object(agent_auth.time, "sleep"), redirect_stdout(StringIO()), self.assertRaises(hosted.HostedError):
            agent_auth.login(self.args)
        self.assertFalse(self.directory.exists())

    def test_foreign_approval_link_and_local_books_fail_closed(self):
        start = {**self.start, "verification_uri_complete": "https://evil.example/connect-agent?user_code=ABCDE-FG234"}
        with patch.object(agent_auth, "_post", return_value=start), self.assertRaises(hosted.HostedError):
            agent_auth.login(self.args)
        self.directory.mkdir()
        ledger = self.directory / "ledger.sqlite"
        ledger.write_bytes(b"synthetic local ledger")
        with patch.object(agent_auth, "_post") as post, self.assertRaises(hosted.HostedError):
            agent_auth.login(self.args)
        post.assert_not_called()
        self.assertEqual(ledger.read_bytes(), b"synthetic local ledger")

    def test_refresh_is_saved_once_and_cannot_be_forwarded_to_another_company(self):
        path = self.home / "credentials.json"
        config = {"endpoint": self.args.endpoint + "/api/v1", "company": "company", "credential_file": str(path)}
        hosted._write_private(path, {"endpoint": config["endpoint"], "company": "company", "expires_at": 0,
                                     "token": "old_access", "refresh_token": "old_refresh"})
        with patch.object(agent_auth, "_post", return_value=self.tokens) as post:
            self.assertEqual(agent_auth.credential_token(config)[0], self.tokens["access_token"])
            self.assertEqual(agent_auth.credential_token(config)[0], self.tokens["access_token"])
            post.assert_called_once()
        with self.assertRaises(hosted.HostedError):
            agent_auth.credential_token({**config, "company": "another-company"})

    def test_revoked_refresh_keeps_binding_and_never_reverts_to_local_books(self):
        path = self.home / "credentials.json"
        config = {"endpoint": self.args.endpoint + "/api/v1", "company": "company", "credential_file": str(path)}
        original = {"endpoint": config["endpoint"], "company": "company", "expires_at": 0,
                    "token": "old_access", "refresh_token": "old_refresh"}
        hosted._write_private(path, original)
        with patch.object(agent_auth, "_post", return_value={"error": "invalid_grant"}), self.assertRaises(hosted.HostedError):
            agent_auth.credential_token(config)
        self.assertEqual(json.loads(path.read_text()), original)

    def test_insecure_credentials_and_symlink_are_rejected(self):
        path = self.home / "credentials.json"
        path.write_text("{}")
        path.chmod(0o644)
        with self.assertRaises(hosted.HostedError):
            agent_auth.credential_token({"credential_file": str(path)})
        link = self.home / "link.json"
        link.symlink_to(path)
        with self.assertRaises(hosted.HostedError):
            agent_auth.credential_token({"credential_file": str(link)})


if __name__ == "__main__":
    unittest.main()
