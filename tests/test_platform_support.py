"""Safety regression checks; these simulate platform detection, not Windows APIs."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bookkeeping import agent_auth, cli, hosted, hosted_migration, platform_support


class PlatformSupportTests(unittest.TestCase):
    def test_windows_login_refused_before_any_network_or_folder(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'new-company'
            with patch.object(platform_support.sys, 'platform', 'win32'), \
                    patch.object(agent_auth, '_post') as post:
                with self.assertRaises(hosted.HostedError) as caught:
                    agent_auth.login(SimpleNamespace(entity=str(folder)))
                self.assertEqual(caught.exception.payload['error']['code'], 'UNSUPPORTED_PLATFORM')
                post.assert_not_called()
                self.assertFalse(folder.exists())

    def test_windows_cli_stops_before_dispatch_and_work_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / 'receipt.json'
            output = StringIO()
            with patch.object(platform_support.sys, 'platform', 'win32'), \
                    patch.object(cli, '_dispatch') as dispatch, redirect_stderr(output):
                self.assertEqual(cli.main(['--work-out', str(receipt), 'cloud', 'login']), 1)
                dispatch.assert_not_called()
            self.assertFalse(receipt.exists())
            self.assertEqual(json.loads(output.getvalue())['error']['code'], 'UNSUPPORTED_PLATFORM')

    def test_windows_help_stays_available(self):
        with patch.object(platform_support.sys, 'platform', 'win32'), redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.main(['--help'])
            self.assertEqual(caught.exception.code, 0)

    def test_direct_migration_capture_fails_before_open(self):
        with patch.object(platform_support.sys, 'platform', 'win32'), \
                patch.object(hosted_migration.os, 'open') as opened:
            with self.assertRaises(hosted_migration.Rejected):
                with hosted_migration._directory('unused'):
                    self.fail('must not enter')
            opened.assert_not_called()

    def test_supported_platforms_not_blocked(self):
        for platform in ['linux', 'darwin']:
            with self.subTest(platform=platform), patch.object(platform_support.sys, 'platform', platform):
                platform_support.require_supported_platform()


class LauncherTests(unittest.TestCase):
    def test_launcher_skips_broken_python3_alias(self):
        import os
        import shlex
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broken = root / 'python3'
            broken.write_text('#!/bin/sh\nexit 1\n')
            broken.chmod(0o755)
            fallback = root / 'python'
            fallback.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
            fallback.chmod(0o755)
            wrapper = Path(__file__).resolve().parents[1] / 'scripts/books'
            result = subprocess.run([str(wrapper), '--help'], cwd=tmp,
                                    env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']},
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage:', result.stdout)

    def test_launcher_reports_missing_interpreter_without_running_cli(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ['python3', 'python', 'py']:
                stub = root / name
                stub.write_text('#!/bin/sh\nexit 1\n')
                stub.chmod(0o755)
            wrapper = Path(__file__).resolve().parents[1] / 'scripts/books'
            result = subprocess.run([str(wrapper), '--help'], cwd=tmp,
                                    env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']},
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('Python 3.11', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
