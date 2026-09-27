"""CLI encoding regression; no installation or network is mocked as a real result."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class Era5CLI(unittest.TestCase):
    def test_setup_progress_with_legacy_redirected_console(self):
        code = '''import runpy,sys
module=runpy.run_path(sys.argv[1],run_name="setup_test")
def fake_setup(root,cancel,progress):
 progress("Установка библиотек: программная проверка")
 return dict(managed=True,python=sys.executable,missing=[],versions={},pyrttov=False)
module["main"].__globals__["ensure_runtime"]=fake_setup
sys.argv=[sys.argv[1],"--state-dir",sys.argv[2]]
module["main"]()
'''
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, PYTHONIOENCODING='cp1252')
            result = subprocess.run([sys.executable, '-c', code, str(ROOT/'scripts/setup_era5.py'), temp],
                                    capture_output=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertIn('Установка библиотек', result.stdout.decode('utf-8'))

if __name__ == '__main__':
    unittest.main()
