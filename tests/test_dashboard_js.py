import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


class DashboardJavaScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is optional for frontend development tests')
    def test_dashboard_behaviors(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which('node'), '--test', 'tests/dashboard.test.cjs'],
                                cwd=root, env={**os.environ, 'PYTHON': sys.executable},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
