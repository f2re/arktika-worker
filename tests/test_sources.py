"""Проверка единого флоу источников. Сетевые запросы здесь не выполняются."""
import tempfile
import unittest
from pathlib import Path

from arktika.sources import earthdata_credentials_from_text, public_earthdata
from test_era5_workflow import workflow_fixture


class EarthdataCredentials(unittest.TestCase):
    def test_token(self):
        value=earthdata_credentials_from_text('abc.def.ghi','token')
        self.assertEqual(value['format'],'token')
        self.assertEqual(value['token'],'abc.def.ghi')
        self.assertTrue(public_earthdata(value)['present'])

    def test_netrc(self):
        value=earthdata_credentials_from_text(
            'machine urs.earthdata.nasa.gov login weather-user password secret-value','netrc')
        self.assertEqual(value['format'],'netrc')
        self.assertEqual(value['login'],'weather-user')
        self.assertEqual(value['password'],'secret-value')

    def test_auto_netrc_with_comment(self):
        value=earthdata_credentials_from_text(
            '# Earthdata credentials\nmachine urs.earthdata.nasa.gov login weather-user password secret-value','auto')
        self.assertEqual(value['format'],'netrc')

    def test_netrc_requires_official_host(self):
        with self.assertRaisesRegex(ValueError,'urs.earthdata.nasa.gov'):
            earthdata_credentials_from_text('machine example.org login user password pass','netrc')

    def test_mixed_netrc_rejected(self):
        with self.assertRaisesRegex(ValueError,'отдельный'):
            earthdata_credentials_from_text(
                'machine urs.earthdata.nasa.gov login user password pass machine example.org login x password y','netrc')

    def test_default_and_macdef_rejected(self):
        for text in ('default login user password pass','macdef init'):
            with self.subTest(text=text),self.assertRaises(ValueError):
                earthdata_credentials_from_text(text,'netrc')

    def test_whitespace_token_rejected(self):
        with self.assertRaises(ValueError):
            earthdata_credentials_from_text('two tokens','token')


class SourceFlow(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app,_,_,_=workflow_fixture(Path(self.tmp.name))

    def tearDown(self):
        self.app.cancel.set()
        self.app.close()
        self.tmp.cleanup()

    def sources(self):
        return {row['id']:row for row in self.app.source_state()['sources']}

    def test_cds_is_shared_by_era5_and_carra2(self):
        before=self.sources()
        self.assertEqual(before['era5']['status'],'credentials_missing')
        self.assertEqual(before['carra2']['status'],'credentials_missing')
        self.app.source_credentials({'provider':'cds','text':'TEST-CDS-TOKEN','format':'token'})
        after=self.sources()
        self.assertEqual(after['era5']['status'],'credentials_present')
        self.assertEqual(after['carra2']['status'],'credentials_present')
        self.app.source_credentials({'provider':'cds','clear':True})
        self.assertEqual(self.sources()['carra2']['status'],'credentials_missing')

    def test_earthdata_is_independent(self):
        self.app.source_credentials({'provider':'earthdata','text':'EARTHDATA-TOKEN','format':'token'})
        sources=self.sources()
        self.assertEqual(sources['merra2']['status'],'credentials_present')
        self.assertEqual(sources['era5']['status'],'credentials_missing')
        self.app.source_credentials({'provider':'earthdata','clear':True})
        self.assertEqual(self.sources()['merra2']['status'],'credentials_missing')

    def test_local_source_is_ready_without_credentials(self):
        self.assertEqual(self.sources()['local']['status'],'ready')


if __name__=='__main__':
    unittest.main()
