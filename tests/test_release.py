"""Контракты упаковки исходников и перехода с раннего формата кэша."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_builder', ROOT/'scripts/build_release.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReleaseContracts(unittest.TestCase):
    def test_version_is_literal(self):
        self.assertEqual(module.version_from_source('__version__ = "0.3.0"'), '0.3.0')

    def test_version_not_executed(self):
        with self.assertRaises((ValueError, SyntaxError)):
            module.version_from_source('__version__ = str(__import__("os").getcwd())')

    def test_invalid_version(self):
        for version in ('', 'v0.3.0', '../x', '0.3', 3):
            with self.subTest(version=version), self.assertRaises(ValueError):
                module.version_from_source('__version__ = ' + repr(version))

    def test_private_paths_rejected(self):
        for name in ('.env', '.netrc', 'state/main.sqlite', '.delivery/patch',
                     '../secret', '/tmp/secret', 'x\\secret', '.venv/a', 'a.pyc'):
            with self.subTest(name=name):
                self.assertFalse(module.safe_name(name))

    def test_source_paths_allowed(self):
        for name in ('static/index.html', '.github/workflows/tests.yml',
                     'config/app.example.json', 'RELEASE_NOTES.md', 'install.sh'):
            self.assertTrue(module.safe_name(name))

    def test_manifest_and_application_version_agree(self):
        self.assertEqual(json.loads((ROOT/'MANIFEST.json').read_text(encoding='utf-8'))['version'],
                         module.version_from_source((ROOT/'arktika/__init__.py').read_text(encoding='utf-8')))

    def test_publication_requires_all_checks(self):
        workflow = (ROOT/'.github/workflows/tests.yml').read_text(encoding='utf-8')
        self.assertIn('needs: [python, era5-install, browser, coefficients, package]', workflow)
        self.assertIn("github.event_name == 'push' && github.ref == 'refs/heads/main'", workflow)
        self.assertIn('--target "$GITHUB_SHA"', workflow)
        self.assertNotIn('--clobber', workflow)


class LegacyCache(unittest.TestCase):
    def test_old_fields_are_preserved_but_not_reinterpreted(self):
        from arktika.workstation import Workstation
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = Workstation(root/'state')
            try:
                folder = root/'state/reanalysis/fields'/('a'*24)
                folder.mkdir(parents=True)
                (folder/'field.json').write_text('{"temporal":"mean_1h"}', encoding='utf-8')
                (folder/'field.nc').write_bytes(b'SYNTHETIC LEGACY FIXTURE; NOT WEATHER DATA')
                state = app.fields_state()
                self.assertEqual(state['legacy_cache_count'], 1)
                self.assertEqual(state['fields'], [])
                self.assertEqual(state['stack'], [])
                self.assertTrue((folder/'field.json').exists())
                self.assertTrue((folder/'field.nc').exists())
            finally:
                app.close()
