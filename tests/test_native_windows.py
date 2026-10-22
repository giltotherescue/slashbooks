"""Run on native Windows: real ACLs, locks, login, migration, and launchers.

Network/model responses are synthetic. Windows filesystem operations are real.
"""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bookkeeping import agent_auth, hosted, remote, file_safety, pinned_files, hosted_adoption, migration_bundle


@unittest.skipUnless(sys.platform == 'win32', 'Requires native Windows APIs')
class NativeWindowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='books native ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tokens = {'access_token': 'sba_synthetic_access', 'refresh_token': 'sbr_synthetic_refresh_token_1234567890',
                       'token_type': 'Bearer', 'company_id': 'synthetic-company', 'expires_in': 3600}

    def test_login_default_folder_refresh_and_no_overwrite(self):
        args = SimpleNamespace(endpoint='https://books.example.com', company='synthetic-company',
                               entity=None, allow_localhost=False, reauthorize=False)
        start = {'device_code': 'synthetic-device', 'user_code': 'ABCDE-FG234', 'interval': 5, 'expires_in': 600,
                 'verification_uri_complete': 'https://books.example.com/connect-agent?user_code=ABCDE-FG234'}
        with patch.object(Path, 'home', return_value=self.root), patch.object(agent_auth.time, 'sleep'), \
                patch.object(hosted.HostedClient, 'request', return_value={'id': 'synthetic-company', 'name': 'Synthetic Company'}), \
                patch.object(agent_auth, '_post', side_effect=[start, self.tokens]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(agent_auth.login(args), 0)
        self.assertIn('"connected": true', output.getvalue())
        for secret in ['sba_synthetic_access', self.tokens['refresh_token']]:
            self.assertNotIn(secret, output.getvalue())
        bindings = list(self.root.glob('Documents/Slashbooks/*/.slashbooks-remote.json'))
        self.assertEqual(len(bindings), 1)
        config = hosted._read_json(bindings[0], private=True)
        credentials = Path(config['credential_file'])
        with os.fdopen(file_safety.open_private(credentials)) as handle:
            self.assertTrue(file_safety.is_private(handle.fileno()))
        stored = hosted._read_json(credentials, private=True)
        hosted._write_private(credentials, {**stored, 'expires_at': 0})
        with patch.object(agent_auth, '_post', return_value=self.tokens) as post:
            self.assertEqual(agent_auth.credential_token(config)[0], self.tokens['access_token'])
            self.assertEqual(agent_auth.credential_token(config)[0], self.tokens['access_token'])
            post.assert_called_once()
        original = bindings[0].read_bytes()
        args.entity = str(bindings[0].parent)
        with patch.object(Path, 'home', return_value=self.root), patch.object(agent_auth, '_post') as post:
            with self.assertRaises(hosted.HostedError) as caught:
                agent_auth.login(args)
            self.assertEqual(caught.exception.payload['error']['code'], 'ALREADY_CONFIGURED')
            post.assert_not_called()
        self.assertEqual(bindings[0].read_bytes(), original)

    def test_exclusive_write_and_broad_acl_rejection(self):
        from bookkeeping import windows_files as win
        import ctypes as c
        path = self.root / 'private.json'
        hosted._write_private(path, {'token': 'synthetic-secret'}, exclusive=True)
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            hosted._write_private(path, {'token': 'replacement'}, exclusive=True)
        self.assertEqual(path.read_bytes(), original)
        # Deliberately grant Everyone read access; the application must refuse it.
        descriptor = win.P()
        win._check(win.ConvertSD('D:P(A;;FA;;;OW)(A;;FR;;;WD)', 1, c.byref(descriptor), None))
        set_security = win._api(win.advapi, 'SetFileSecurityW', [win.w.LPCWSTR, win.DWORD, win.P])
        try:
            win._check(set_security(str(path), 4 | 0x80000000, descriptor))
        finally:
            win.LocalFree(descriptor)
        with self.assertRaises(hosted.HostedError):
            hosted._read_json(path, private=True)

    def test_lock_blocks_another_process_then_releases(self):
        path = self.root / 'state.lock'
        acquired = self.root / 'acquired'
        script = 'from pathlib import Path; from bookkeeping.file_safety import private_lock; import sys\nPath(sys.argv[2] + ".ready").write_text("ready")\nwith private_lock(Path(sys.argv[1])): Path(sys.argv[2]).write_text("yes")'
        with file_safety.private_lock(path):
            process = subprocess.Popen([sys.executable, '-c', script, str(path), str(acquired)],
                                       env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
            try:
                deadline = time.monotonic() + 10
                while not Path(str(acquired) + '.ready').exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(Path(str(acquired) + '.ready').exists())
                time.sleep(0.2)
                self.assertIsNone(process.poll())
                self.assertFalse(acquired.exists())
            except BaseException:
                process.kill()
                process.wait()
                raise
        self.assertEqual(process.wait(timeout=15), 0)
        self.assertEqual(acquired.read_text(), 'yes')

    def test_migration_private_io_and_directory_pinning(self):
        directory = self.root / 'source'
        directory.mkdir()
        file = directory / 'snapshot.bin'
        migration_bundle._write_exclusive(file, b'synthetic')
        self.assertEqual(hosted_adoption._read_bytes(file, 100, private=True), b'synthetic')
        with pinned_files.directory(directory) as (parent, _):
            self.assertIn('snapshot.bin', pinned_files.names(parent))
            with self.assertRaises(OSError):
                directory.rename(self.root / 'moved')
            with self.assertRaises(FileExistsError):
                pinned_files.open_child(parent, file.name, create=True)

    def test_junction_rejected(self):
        target = self.root / 'target'
        target.mkdir()
        junction = self.root / 'junction'
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)], check=True, capture_output=True)
        with self.assertRaises(hosted.HostedError):
            remote._no_symlinks(junction / 'data.json')
        with self.assertRaises(OSError):
            with pinned_files.directory(junction):
                self.fail('junction must not be traversed')
        junction.rmdir()

    def test_native_launcher_and_local_books(self):
        repo = Path(__file__).resolve().parents[1]
        launcher = repo / 'scripts/books.cmd'
        result = subprocess.run(['cmd', '/c', str(launcher), '--help'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, '-m', 'bookkeeping.cli', 'demo', 'init', str(self.root / 'demo')],
                                env={**os.environ, 'PYTHONPATH': str(repo / 'src')}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, '-m', 'bookkeeping.cli', 'report', 'trial-balance', '--as-of', '2026-12-31', '--entity', str(self.root / 'demo')],
                                env={**os.environ, 'PYTHONPATH': str(repo / 'src')}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_full_migration_export_and_private_snapshot_read(self):
        from tests.test_hosted_migration import MigrationPreflightTests
        fixture = MigrationPreflightTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        target = self.root / 'export.zip'
        result = migration_bundle.build_bundle(fixture.root, target)
        self.assertEqual(result['status'], 'exported', result)
        self.assertTrue(result['bundle_created'])
        self.assertIn('manifest', migration_bundle.load_bundle(target))
        self.assertEqual(hosted_adoption._read_bytes(target, target.stat().st_size, private=True), target.read_bytes())

    def test_git_bash_launcher_and_skill_cmd(self):
        repo = Path(__file__).resolve().parents[1]
        bash = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Git/bin/bash.exe'
        self.assertTrue(bash.exists(), 'Native CI must include Git Bash to exercise the reported installation path')
        result = subprocess.run([str(bash), str(repo / 'scripts/books').replace('\\', '/'), '--help'],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(['cmd', '/c', str(repo / 'skills/books-cloud/scripts/books.cmd'), '--help'],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cloud_upload_download_and_private_receipts(self):
        from bookkeeping import cli
        from tests.test_remote import API, TOKEN, artifact
        import base64
        api = API()
        self.addCleanup(api.close)
        entity = self.root / 'online company'
        entity.mkdir()

        def invoke(*args):
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = cli.main(list(args))
            self.assertEqual(code, 0, output.getvalue() + error.getvalue())
            self.assertNotIn(TOKEN, output.getvalue() + error.getvalue())

        with patch.dict(os.environ, {'BOOKS_API_TOKEN': TOKEN}), patch.object(cli, 'load_dotenv'):
            invoke('cloud', 'configure', '--endpoint', api.endpoint, '--company', 'company-A',
                   '--allow-localhost', '--entity', str(entity))
            source = self.root / 'source.json'
            source.write_bytes(b'{"transactions": []}')
            invoke('ingest', str(source), '--source', 'synthetic', '--entity', str(entity))
            self.assertEqual(base64.b64decode(api.calls[-1]['body']['inputs'][0]['data_base64']), source.read_bytes())
            output = self.root / 'pnl.json'
            api.artifacts = [artifact('@output/output/pnl.json')]
            invoke('report', 'pnl', '--from', '2026-01-01', '--to', '2026-03-31',
                   '--output', str(output), '--entity', str(entity))
            self.assertEqual(output.read_bytes(), b'synthetic output\n')
            with os.fdopen(file_safety.open_private(output)) as handle:
                self.assertTrue(file_safety.is_private(handle.fileno()))
            receipts = list((entity / '.slashbooks-remote-receipts').glob('*.json'))
            self.assertEqual(len(receipts), 2)
            for receipt in receipts:
                self.assertIn('response', hosted._read_json(receipt, private=True))
            self.assertFalse((entity / 'ledger.sqlite').exists())
