"""Синтетические регрессии планировщика и шкал; не проверка метеоточных наблюдений."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
from arktika.model import normalize_item
from arktika.processing import calibration, build_product
from arktika.profiles import cloud_top
from arktika.radiometry import metadata_scale, two_points, fit_pairs
from arktika.workstation import Workstation
from test_science import fixture
from test_archive import archive_assets, rgb_fixture
from test_catalog_integration import remote_fixture

STAMP='2026-01-01T00:00:00Z'

class RadiometryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.scene=fixture(self.root)
    def tearDown(self):self.temp.cleanup()
    def ds(self):return rasterio.open(self.scene['channels']['9']['path'])
    def tags(self,unit='K',scale=1.,offset=0.,**tags):
        with rasterio.open(self.scene['channels']['9']['path'],'r+') as ds:
            ds.set_band_unit(1,unit);ds.scales=(scale,);ds.offsets=(offset,)
            if tags:ds.update_tags(1,**tags)
    def test_unknown_does_not_infer_kelvin(self):
        with self.ds() as ds:self.assertIsNone(metadata_scale(ds))
    def test_explicit_encoding(self):
        self.tags(scale=.01,offset=200)
        with self.ds() as ds:r=metadata_scale(ds)
        self.assertEqual((r['scale'],r['offset'],r['status']),(.01,200,'metadata'))
    def test_celsius_to_kelvin(self):
        self.tags('C',.1,-80)
        with self.ds() as ds:r=metadata_scale(ds)
        self.assertAlmostEqual(r['offset'],193.15)
    def test_stac_only(self):
        with self.ds() as ds:r=metadata_scale(ds,[dict(unit='K',scale=.1,offset=100)])
        self.assertEqual(r['scale'],.1);self.assertIn('STAC',r['reference'])
    def test_conflict_rejected(self):
        self.tags()
        with self.ds() as ds:
            with self.assertRaises(ValueError):metadata_scale(ds,[dict(unit='K',scale=.1)])
    def test_tag_encoding(self):
        self.tags(scale_factor='.1',add_offset='180')
        with self.ds() as ds:r=metadata_scale(ds)
        self.assertEqual((r['scale'],r['offset']),(.1,180))
    def test_internal_conflict_rejected(self):
        self.tags(scale=.2,scale_factor='.1')
        with self.ds() as ds:
            with self.assertRaises(ValueError):metadata_scale(ds)
    def test_no_temperature_from_visible_channel(self):
        self.tags()
        with self.ds() as ds:self.assertEqual(calibration(ds,1,{'mode':'unknown'})[2],'DN')
    def test_two_anchors(self):
        r=two_points(100,220,500,300)
        self.assertEqual((r['scale'],r['offset']),(.2,200))
    def test_anchors_allow_reversed_encoding(self):
        self.assertEqual(two_points(500,220,100,300)['scale'],-.2)
    def test_bad_anchors(self):
        for v in ((1,220,1,300),(1,220,2,220),(1,-5,2,300),(True,220,2,300)):
            with self.subTest(v=v),self.assertRaises(ValueError):two_points(*v)
    def test_fit_with_group_exclusion(self):
        text='dn,temperature_k,group\n'+''.join(f'{x},{200+.2*x},{i//3}\n' for i,x in enumerate(range(100,1000,100)))
        r=fit_pairs(text)
        self.assertAlmostEqual(r['scale'],.2);self.assertAlmostEqual(r['offset'],200)
        self.assertLess(r['rmse_fit_k'],1e-10);self.assertLess(r['rmse_group_cv_k'],1e-10)
        self.assertEqual(r['groups'],3)
    def test_fit_celsius_semicolon(self):
        text='dn;temperature_c\n'+''.join(f'{x};{200+.2*x-273.15}\n' for x in range(100,800,100))
        self.assertAlmostEqual(fit_pairs(text)['offset'],200)
    def test_bad_csv_rejected(self):
        for text in ('dn,temperature_k\n1,250','dn,temperature_k,temperature_c\n', 'dn,temperature_k\n'+('1,250\n'*6)):
            with self.subTest(text=text),self.assertRaises(ValueError):fit_pairs(text)
    def test_root_at_exact_level_is_unique(self):
        r=cloud_top(dict(cloud_confirmed=True,temperature_units='K',height_units='m',height=[-500,.1,1000],temperature=[280,270,260],brightness_temperature=270))
        self.assertEqual(r['candidate_heights_m'],[.1]);self.assertFalse(r['ambiguous'])
    def test_stac_band_encoding_survives_normalization(self):
        raw=dict(type='Feature',id='SYNTHETIC',properties={'platform':'ARCM2','datetime':STAMP},assets={'ch09':{'href':'https://s3.gptl.ru/SYNTHETIC/A2_20260101000000_ch09.tif','raster:bands':[{'unit':'K','scale':.1,'offset':100}]}})
        _,assets=normalize_item(raw)
        self.assertEqual(assets[0]['raster_bands'][0]['scale'],.1)
        raw['assets']['ch09']['raster:bands']=True
        self.assertEqual(normalize_item(raw)[1][0]['raster_bands'],[])

class ProductFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.local=self.root/'local';self.local.mkdir();self.source=fixture(self.local)
        self.app=Workstation(self.root/'state');self.app.scan_local(self.local)
        self.scene=self.app.scenes()[0]
    def tearDown(self):self.app.close();self.temp.cleanup()
    def plan(self,product='micro24',**extra):
        return self.app.product_plan(dict(product=product,platform='ARCM2',time=STAMP,**extra))
    def scale(self,**extra):
        return dict(scene=self.scene['id'],channels=[7,9,10],method='linear',scale=1,offset=0,acknowledged=True,**extra)
    def test_missing_time_is_actionable(self):
        self.assertEqual(self.app.product_plan({'product':'cth'})['status'],'needs_time')
    def test_no_blanket_ten_channel_requirement(self):
        p=self.plan();self.assertEqual(p['required'],[7,9,10]);self.assertEqual(p['missing'],[])
    def test_unknown_scale_is_not_unavailable(self):
        p=self.plan();self.assertEqual(p['status'],'calibration');self.assertEqual(p['calibration_needed'],[7,9,10])
    def test_single_channel_dn_is_ready(self):self.assertEqual(self.plan('channel',channel=4)['status'],'ready')
    def test_metadata_makes_rgb_ready(self):
        fixture(self.local,units=True)
        self.assertEqual(self.plan()['status'],'ready')
    def test_only_missing_channel_is_queued(self):
        Path(self.source['channels']['7']['path']).unlink()
        record,assets=remote_fixture(STAMP)
        for a in assets:a['filename']=a['filename'].replace('01010000','01000000')
        self.app.store.upsert([record],assets)
        p=self.plan();self.assertEqual(p['status'],'download');self.assertEqual(p['missing'],[7]);self.assertEqual(len(p['download_ids']),1)
    def test_research_scaling_unblocks_product(self):
        self.app.save_scale(self.scale());self.assertEqual(self.plan()['status'],'ready')
        self.assertTrue(all(c['status']=='assumed' for c in self.plan()['calibration'].values()))
    def test_scale_requires_acknowledgement(self):
        d=self.scale();d['acknowledged']=False
        with self.assertRaises(ValueError):self.app.save_scale(d)
    def test_scope_is_scene_by_default(self):
        self.app.save_scale(self.scale())
        scene=self.app.scene(self.scene['id']);scene['id']='ARCM2_other';scene['time']='2026-01-01T00:15:00Z'
        self.assertEqual(self.app.radiometry_config(scene)[0]['channels'],{})
    def test_platform_scope_does_not_cross_instrument(self):
        self.app.save_scale(self.scale(scope='platform'))
        scene=self.app.scene(self.scene['id']);scene['id']='ARCM1_other';scene['platform']='ARCM1'
        self.assertEqual(self.app.radiometry_config(scene)[0]['channels'],{})
    def test_return_to_metadata_clears_selected_override(self):
        self.app.save_scale(self.scale())
        d=self.scale();d['method']='auto';d['channels']=[9];self.app.save_scale(d)
        p=self.plan();self.assertEqual(p['calibration_needed'],[9])
    def test_document_requires_source(self):
        d=self.scale();d['method']='declared'
        with self.assertRaises(ValueError):self.app.save_scale(d)
    def test_visible_scale_forbidden(self):
        d=self.scale();d['channels']=[1]
        with self.assertRaises(ValueError):self.app.save_scale(d)
    def test_preview_does_not_modify_settings(self):
        d=self.scale();d['product']='micro24';d['preset']='barents'
        before=self.app.store.setting('scale_profiles',{})
        r=self.app.preview_scale_image(d)
        self.assertTrue(r['image'].startswith('data:image/png;base64,'));self.assertFalse(r['persisted'])
        self.assertEqual(before,self.app.store.setting('scale_profiles',{}));self.assertEqual(self.app.products(),[])
    def test_product_freezes_scale_provenance(self):
        self.app.save_scale(self.scale())
        scene=self.app.scene(self.scene['id']);cal,_=self.app.radiometry_config(scene)
        r=build_product(scene,self.app.product_root,dict(product='micro24',preset='barents',width=256),cal)
        d=self.scale();d['offset']=5;self.app.save_scale(d)
        self.assertEqual(self.app.product(r['id'])['calibration_config']['channels']['9']['offset'],0)
        self.assertEqual(r['calibration_status'],'assumed')
    def test_conflicting_stac_cannot_fall_back_to_tiff(self):
        fixture(self.local,units=True);scene=self.app.scene(self.scene['id'])
        scene['channels']['9']['raster_bands']=[dict(unit='K',scale=.1)]
        cal,issues=self.app.radiometry_config(scene)
        self.assertIn('9',issues)
        with rasterio.open(scene['channels']['9']['path']) as ds:self.assertEqual(calibration(ds,9,cal)[2],'DN')
    def test_height_plan_reuses_physical_prerequisites(self):
        p=self.plan('cth');self.assertEqual(p['required'],[7,9,10]);self.assertTrue(p['needs_profile']);self.assertEqual(p['display_product'],'channel')
    def test_invalid_channel_never_coerced(self):
        for ch in (True,3.8,0,11):
            with self.subTest(ch=ch),self.assertRaises(ValueError):self.plan('channel',channel=ch)
    def test_archive_preserves_selected_projection(self):
        record,assets=archive_assets();self.app.store.upsert([record],assets)
        for a in assets:
            path=self.local/a['filename'];rgb_fixture(path,a['epsg'])
            self.app.register_file(path,a)
        scene=self.app.scenes(record['time'][:10])[0]
        for image in scene['composites']:
            p=self.app.product_plan(dict(product='archive_rgb',platform=record['platform'],time=record['time'],composite=image['id']))
            self.assertEqual(p['composite'],image['id']);self.assertEqual(p['status'],'ready')
    def test_bad_save_keeps_previous_scale(self):
        self.app.save_scale(self.scale());before=copy.deepcopy(self.app.store.setting('scale_profiles'))
        d=self.scale();d['scale']=0
        with self.assertRaises(ValueError):self.app.save_scale(d)
        self.assertEqual(before,self.app.store.setting('scale_profiles'))
    def test_scale_inventory_redacts_local_path(self):
        result=self.app.scale_inventory({'scene':self.scene['id']})
        self.assertNotIn(str(self.root),json.dumps(result));self.assertEqual(len(result['channels']),7)
    def test_motion_uses_both_scoped_scales(self):
        second=dict(self.app.scene(self.scene['id']),id='other',time='2026-01-01T00:15:00Z')
        first=self.app.scene(self.scene['id'])
        with patch.object(self.app,'scene',side_effect=[first,second]),patch.object(self.app,'start') as start:
            self.app.motion(dict(first=first['id'],second=second['id']))
            self.assertTrue(start.called)

if __name__=='__main__':unittest.main()
