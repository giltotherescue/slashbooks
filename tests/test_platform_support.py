"""Launcher interpreter selection regression tests."""
from pathlib import Path
import sys
import tempfile
import unittest


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
