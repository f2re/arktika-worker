"""Интеграционные регрессии для запуска внутри полного arktika-worker."""
import json
from pathlib import Path
import tempfile
import unittest

from arktika.catalog import BUNDLES
from arktika.model import normalize_item
from arktika.products import PRODUCTS
from arktika.store import Store
from arktika.workstation import Workstation
from test_science import fixture


def remote_fixture(stamp='2026-01-01T01:00:00Z', with_assets=True):
    raw=dict(type='Feature',id='SYNTHETIC-CATALOG-'+stamp,properties={
        'platform':'ARCM2','datetime':stamp,'processing:level':'L2IR'},assets={})
    if with_assets:
        for channel in (7,9,10):
            raw['assets'][f'band_ch{channel:02d}']=dict(
                href=f'https://s3.gptl.ru/SYNTHETIC-TEST/A2_20260101010000_ch{channel:02d}.tif',
                type='image/tiff',**{'file:size':1000,'proj:epsg':4326})
    return normalize_item(raw,'synthetic-software-test')


class CatalogIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.local=self.root/'local';self.local.mkdir();fixture(self.local)
        self.app=Workstation(self.root/'state');self.app.scan_local(self.local)
        record,assets=remote_fixture();self.app.store.upsert([record],assets)
    def tearDown(self):
        self.app.close();self.temp.cleanup()
    def test_combined_sessions(self):
        r=self.app.sessions('2026-01-01',dict(platform='',category='all',channel=0,epsg=0,query=''),0,60)
        self.assertEqual(r['total'],2)
        self.assertTrue(any(s['local_id'] for s in r['sessions']))
        self.assertTrue(any(s['catalog_channels']==[7,9,10] for s in r['sessions']))
    def test_no_uri_record_retained(self):
        record,assets=remote_fixture('2026-01-01T02:00:00Z',False)
        self.app.store.upsert([record],assets)
        r=self.app.sessions('2026-01-01',{})
        self.assertEqual(r['total'],3)
        row=next(s for s in r['sessions'] if s['time']==record['time'])
        self.assertTrue(row['no_uri']);self.assertEqual(row['record_count'],1)
    def test_missing_file_not_ready(self):
        p=self.local/'A2_20260101000000_ch09.tif';contents=p.read_bytes();p.unlink()
        scenes=self.app.scenes('2026-01-01')
        self.assertNotIn(9,[c['channel'] for c in scenes[0]['channels']])
        row=self.app.session('ARCM2','2026-01-01T00:00:00Z')
        self.assertEqual(row['channel_inventory'][8]['state'],'absent')
        p.write_bytes(contents)
        self.assertIn(9,[c['channel'] for c in self.app.scenes('2026-01-01')[0]['channels']])
    def test_session_has_no_runtime_paths(self):
        row=self.app.session('ARCM2','2026-01-01T00:00:00Z')
        self.assertNotIn(str(self.root),json.dumps(row))
        self.assertEqual(row['channel_inventory'][8]['state'],'local')
    def test_bundle_channels_match_existing_products(self):
        products={p['id']:p for p in PRODUCTS}
        for bundle in BUNDLES:
            if bundle['id'] in products and bundle['channels'] is not None:
                self.assertEqual(bundle['channels'],products[bundle['id']]['channels'])


class MissingDownloadTests(unittest.TestCase):
    def test_deleted_done_download_can_be_queued_again(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'state')
            try:
                record,assets=remote_fixture();asset=assets[0];store.upsert([record],assets)
                destination=Path(temp)/asset['filename']
                store.enqueue(asset,destination);store.update_job(asset['id'],state='done',done=1000,sha256='synthetic')
                store.enqueue(asset,destination)
                self.assertEqual(store.job(asset['id'])['state'],'queued')
                self.assertEqual(store.job(asset['id'])['done'],0)
                self.assertEqual(store.job(asset['id'])['sha256'],'')
            finally:
                store.close()
