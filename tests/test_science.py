"""Искусственные массивы только для проверки алгоритмов, не метеонаблюдения."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from PIL import Image
from arktika.geo import grid,solar_elevation,densify_route,project_points,map_context
from arktika.processing import calibration,validate_calibration,stretch,build_product,CalibrationError
from arktika.profiles import integrate,cloud_top
from arktika.motion import block_match,rotations
from arktika.workstation import Workstation
from arktika.auth import AuthClient
from server import ROOT

def fixture(folder,units=False):
 channels={};values={4:265,5:244,6:250,7:271,8:258,9:275,10:274}
 for ch,value in values.items():
  p=Path(folder)/('A2_20260101000000_ch%02d.tif'%ch)
  a=np.full((64,64),value,dtype='uint16');a[:2]=0
  with rasterio.open(p,'w',driver='GTiff',height=64,width=64,count=1,dtype='uint16',crs='EPSG:4326',transform=from_bounds(20,65,50,85,64,64),nodata=0) as ds:
   ds.write(a,1);ds.update_tags(source='SYNTHETIC UNIT TEST ONLY, NOT OBSERVATIONS')
   if units:ds.set_band_unit(1,'K')
  channels[str(ch)]={'path':str(p),'filename':p.name,'channel':ch}
 return dict(id='ARCM2_20260101000000',platform='ARCM2',time='2026-01-01T00:00:00Z',channels=channels,time_assumed=True)
class ScienceTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.scene=fixture(self.root)
 def tearDown(self):self.tmp.cleanup()
 def build(self,product='channel',cal=None):return build_product(self.scene,self.root/'products',dict(product=product,preset='barents',width=256,channel=9),cal or {'mode':'unknown'})
 def test_msu_gs_a_nominal_channel_centres(self):
  from arktika.products import CHANNELS
  self.assertEqual([w for _,w,_,_ in CHANNELS],[.57,.72,.86,3.75,6.35,8.,8.7,9.7,10.7,11.7])
 def test_uint16_not_kelvin(self):
  with rasterio.open(self.scene['channels']['9']['path']) as ds:self.assertEqual(calibration(ds,9,{'mode':'unknown'})[2],'DN')
 def test_explicit_kelvin_metadata(self):
  fixture(self.root,True)
  with rasterio.open(self.scene['channels']['9']['path']) as ds:self.assertEqual(calibration(ds,9,{'mode':'unknown'})[2],'K')
 def test_rgb_requires_calibration(self):
  with self.assertRaises(CalibrationError):self.build('micro24')
 def test_coefficients_require_reference(self):
  with self.assertRaises(CalibrationError):validate_calibration({'mode':'declared','channels':{'9':{'scale':1,'offset':0,'units':'K'}}})
 def test_coefficients_reject_nan(self):
  with self.assertRaises(CalibrationError):validate_calibration({'mode':'declared','reference':'test','channels':{'9':{'scale':float('nan'),'offset':0,'units':'K'}}})
 def test_radiance_not_temperature(self):
  with self.assertRaises(CalibrationError):validate_calibration({'mode':'declared','reference':'test','channels':{'9':{'scale':1,'offset':0,'units':'W/m2'}}})
 def test_stretch_and_gamma(self):np.testing.assert_allclose(stretch(np.array([-1,0,.25,1,2]),0,1,2),[0,0,.5,1,1])
 def test_stretch_reject_reverse(self):
  with self.assertRaises(ValueError):stretch(np.array([1]),2,1)
 def test_channel_keeps_dn(self):
  p=self.build();self.assertEqual(p['legend']['units'],'DN');self.assertEqual(p['legend']['stats']['min'],275)
 def test_source_not_modified(self):
  from arktika.download import digest
  p=self.scene['channels']['9']['path'];before=digest(p);self.build();self.assertEqual(before,digest(p))
 def test_micro_components(self):
  p=self.build('micro24',{'mode':'assumed'});image=np.array(Image.open(self.root/'products'/p['id']/'map.png'));v=image[image[:,:,3]>0]
  self.assertGreater(len(v),0);np.testing.assert_allclose(v[:,:3],np.tile([128,170,204],(len(v),1)),atol=1)
 def test_calibration_persisted(self):
  p=self.build('micro24',{'mode':'assumed'});self.assertEqual(p['calibration_status'],'assumed');self.assertEqual(p['legend']['status'],'assumed')
 def test_outputs_have_geo_and_alpha(self):
  p=self.build('micro24',{'mode':'assumed'})
  with rasterio.open(self.root/'products'/p['id']/'display.tif') as ds:self.assertEqual(ds.crs.to_epsg(),3995);self.assertEqual(ds.count,4);self.assertEqual(ds.colorinterp[-1].name,'alpha')
 def test_window_difference_signed(self):
  p=self.build('difference',{'mode':'assumed'});self.assertEqual(p['legend']['stats']['min'],-1)
 def test_night_masks_day(self):
  self.scene['time']='2026-06-21T12:00:00Z';p=self.build('night',{'mode':'assumed'});self.assertEqual(p['legend']['valid_pixels'],0)
 def test_night_winter_valid(self):
  p=self.build('night',{'mode':'assumed'});self.assertGreater(p['legend']['valid_pixels'],0)
 def test_indicators_not_hazards(self):
  p=self.build('indicators',{'mode':'assumed'});self.assertEqual(p['legend']['units'],'class');self.assertIn('Не карта ОЯ',p['legend']['meaning'])
 def test_missing_channel(self):
  del self.scene['channels']['7']
  with self.assertRaises(ValueError):self.build('micro24',{'mode':'assumed'})
 def test_invalid_product(self):
  with self.assertRaises(ValueError):self.build('pmc')
 def test_export_no_source_path(self):
  p=self.build();self.assertNotIn(self.tmp.name,json.dumps(p));self.assertTrue((self.root/'products'/p['id']/'report.html').exists())
 def test_arctic_pole_at_center(self):
  g=grid('arctic',256);x,y=project_points(g,[(0,90)])[0];self.assertAlmostEqual(x,128);self.assertAlmostEqual(y,128)
 def test_geographic_dateline(self):
  g=grid('geographic',360);points=project_points(g,[(-180,60),(180,60)]);self.assertAlmostEqual(points[0][0],0);self.assertAlmostEqual(points[1][0],360)
 def test_solar_day_and_night(self):
  self.assertGreater(solar_elevation(0,0,'2026-03-20T12:00:00Z'),85);self.assertLess(solar_elevation(0,0,'2026-03-20T00:00:00Z'),-85)
 def test_solar_naive_rejected(self):
  with self.assertRaises(ValueError):solar_elevation(0,0,'2026-03-20T12:00:00')
 def test_geodesic_route(self):
  r=densify_route([[0,0],[1,0]],25,100,'2026-01-01T00:00:00Z');self.assertAlmostEqual(r[-1]['distance_km'],111.3195,places=3);self.assertEqual(len(r),6)
 def test_route_dateline_short_way(self):
  r=densify_route([[179,70],[-179,70]],25);self.assertLess(r[-1]['distance_km'],100)
 def test_route_bad_coords(self):
  with self.assertRaises(ValueError):densify_route([[300,70],[2,71]])
 def test_route_requires_two(self):
  with self.assertRaises(ValueError):densify_route([[0,0]])
 def test_route_eta_not_future_field(self):
  from arktika.routes import route_profile
  r=route_profile(self.scene,[[30,70],[40,75]],{'mode':'unknown'},departure='2026-01-01T04:00:00Z')
  self.assertFalse(r['samples'][0]['forecast']);self.assertIn('устарел',r['samples'][0]['status']);self.assertEqual(r['units']['9']['unit'],'DN')
 def test_profile_integral_constant(self):
  d=dict(pressure=[10000,50000,100000],specific_humidity=[.005]*3,pressure_units='Pa',humidity_units='kg/kg',source='SYNTHETIC TEST',valid_time='2026-01-01T00:00:00Z')
  r=integrate(d);self.assertAlmostEqual(r['vapor_mm'],450/9.80665)
 def test_profile_reversed_levels(self):
  d=dict(pressure=[100000,50000,10000],specific_humidity=[.005]*3,pressure_units='Pa',humidity_units='kg/kg',source='test',valid_time='2026-01-01T00:00:00Z')
  self.assertAlmostEqual(integrate(d)['vapor_mm'],450/9.80665)
 def test_profile_unit_guard(self):
  with self.assertRaises(ValueError):integrate({'pressure_units':'hPa'})
 def test_cloud_height_ambiguous(self):
  r=cloud_top(dict(cloud_confirmed=True,temperature_units='K',height_units='m',height=[0,1000,2000],temperature=[280,270,280],brightness_temperature=275));self.assertEqual(r['candidate_heights_m'],[500,1500]);self.assertTrue(r['ambiguous'])
 def test_cloud_height_requires_mask(self):
  with self.assertRaises(ValueError):cloud_top({'cloud_confirmed':False})
 def test_translation_matching(self):
  rng=np.random.default_rng(12);a=rng.normal(size=(100,100));b=np.roll(np.roll(a,2,axis=0),3,axis=1);v=block_match(a,b);self.assertGreater(len(v),3);self.assertTrue(all(x['dx']==3 and x['dy']==2 for x in v));self.assertEqual(rotations(v,900),[])
 def test_flat_texture_no_vectors(self):self.assertEqual(block_match(np.ones((100,100)),np.ones((100,100))),[])
 def test_pmc_not_claimed_by_rotation(self):
  v=[dict(x=x,y=y,dx=-(50-y)*.1,dy=-(x-50)*.1) for x in range(20,90,20) for y in range(20,90,20)]
  for c in rotations(v,900):self.assertEqual(c['status'],'candidate_rotation_not_pmc')
 def test_import_actual_format(self):
  a=Workstation(self.root/'state')
  try:r=a.scan_local(self.root);self.assertEqual(r['files'],7);self.assertEqual(len(a.scenes()),1);self.assertNotIn('path',json.dumps(a.scenes()))
  finally:a.close()
 def test_oauth_state_without_request(self):
  c=AuthClient()
  with self.assertRaises(ValueError):c.callback('code','not-pending')
 def test_oauth_redirect_restricted(self):
  c=AuthClient()
  with self.assertRaises(ValueError):c.configure_oauth(dict(client_id='test',redirect_uri='https://evil.invalid/callback'))
 def test_bearer_alone_not_refreshable(self):
  with self.assertRaises(ValueError):AuthClient('test').refresh(force=True)
 def test_pkce_state_and_verifier(self):
  c=AuthClient(oauth=dict(client_id='test',redirect_uri='http://127.0.0.1:8765/oauth/callback'));url=c.begin();self.assertIn('code_challenge_method=S256',url);self.assertEqual(len(c.pending),1)
if __name__=='__main__':unittest.main()

class SecurityIntegrationTests(unittest.TestCase):
 def test_refresh_secret_redacted(self):
  from arktika.network import redact
  c=AuthClient();c.replace('token','opaque-refresh-SHOULD-NOT-LEAK');self.assertNotIn('SHOULD-NOT-LEAK',redact('opaque-refresh-SHOULD-NOT-LEAK'))
 def test_profile_partial_column_not_total_claim(self):
  r=integrate(dict(pressure=[50000,70000],specific_humidity=[.005,.005],pressure_units='Pa',humidity_units='kg/kg',source='test',valid_time='2026-01-01T00:00:00Z'))
  self.assertEqual(r['pressure_range_pa'],[50000,70000]);self.assertIn('не обязательно полный',r['note'])
 def test_cloud_flat_profile_is_ambiguous(self):
  r=cloud_top(dict(cloud_confirmed=True,temperature_units='K',height_units='m',height=[0,1000],temperature=[270,270],brightness_temperature=270));self.assertTrue(r['ambiguous']);self.assertEqual(r['ambiguous_layers_m'],[[0,1000]])
 def test_declared_coefficients_do_not_accept_token_keys(self):
  with self.assertRaises(CalibrationError):validate_calibration(dict(mode='unknown',token='private'))
 def test_export_bundles_legend(self):
  import zipfile
  with tempfile.TemporaryDirectory() as folder:
   scene=fixture(folder);a=Workstation(Path(folder)/'state')
   try:
    a.scan_local(folder);p=build_product(scene,a.product_root,dict(product='channel',channel=9,preset='barents',width=256),{'mode':'unknown'})
    import io
    z=zipfile.ZipFile(io.BytesIO(a.export(p['id'])));self.assertIn('legend.json',z.namelist());self.assertIn('product.json',z.namelist());self.assertIn('report.html',z.namelist());self.assertIn('values.tif',z.namelist())
   finally:a.close()
 def test_product_path_traversal_rejected(self):
  with tempfile.TemporaryDirectory() as folder:
   a=Workstation(folder)
   try:
    with self.assertRaises(ValueError):a.product('../../private')
   finally:a.close()
 def test_modified_input_rejected_for_route(self):
  with tempfile.TemporaryDirectory() as folder:
   scene=fixture(folder);a=Workstation(Path(folder)/'state')
   try:
    a.scan_local(folder);p=build_product(scene,a.product_root,dict(product='channel',channel=9,preset='barents',width=256),{'mode':'unknown'})
    with rasterio.open(scene['channels']['9']['path'],'r+') as ds:ds.write(np.ones((64,64),dtype='uint16')*260,1)
    with self.assertRaises(ValueError):a.frozen_scene(p)
   finally:a.close()
 def test_rotation_signal_detected(self):
  v=[dict(x=x,y=y,dx=(y-50)*.1,dy=-(x-50)*.1) for x in range(20,90,20) for y in range(20,90,20)]
  self.assertGreater(len(rotations(v,900)),0)
 def test_pure_expansion_is_not_rotation(self):
  v=[dict(x=x,y=y,dx=(x-50)*.1,dy=(y-50)*.1) for x in range(20,90,20) for y in range(20,90,20)]
  self.assertEqual(rotations(v,900),[])
