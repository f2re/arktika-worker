"""Synthetic SOFTWARE fixtures only. No tests below establish meteorological skill or live access."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
try:
    import xarray as xr
    import scipy
    import contourpy
    HAS_FIELDS=True
except ImportError:
    HAS_FIELDS=False

from arktika.reanalysis.catalog import request, plan, catalog, identity
from arktika.reanalysis.normalize import normalize, convert
from arktika.reanalysis.render import difference, sample, style, render
from arktika.reanalysis.storage import save, load
from arktika.reanalysis.adapters import find_opendap, nasa_url, nasa_session, read_local
from arktika.workstation import Workstation


def field_request(source='era5', field='t', **kw):
    body=dict(source=source,field=field,time='2024-01-01T00:00:00Z',level=850,area=[73,25,68,36])
    body.update(kw)
    return request(body)


def fixture(source='era5',field='t',value=273.15):
    r=field_request(source,field)
    spec=plan(r)
    provider_names=spec.get('variables',spec.get('request',{}).get('variable'))
    lon=np.arange(28.,33.01,.25);lat=np.arange(69.,72.01,.25)
    shape=(1,1,len(lat),len(lon))
    ds=xr.Dataset({name:(('time','level','lat','lon'),np.full(shape,value,dtype='float32')) for name in provider_names},
        coords={'time':[np.datetime64('2024-01-01T00:00:00')],'level':[850.], 'lat':lat,'lon':lon})
    ds.level.attrs['units']='hPa';ds.lat.attrs['units']='degrees_north';ds.lon.attrs['units']='degrees_east'
    for name in provider_names:ds[name].attrs['units']='K' if field=='t' else 'm s-1'
    return ds,r


class RequestTests(unittest.TestCase):
    def test_actual_carra_contract(self):
        p=plan(field_request('carra2'))
        self.assertEqual(p['dataset'],'reanalysis-pan-carra');self.assertEqual(p['request']['level_location'],['850'])
        self.assertEqual(p['request']['product_type'],'analysis');self.assertEqual(p['request']['level_type'],'pressure_levels')
        self.assertNotIn('pressure_level',p['request'])
    def test_era5_contract(self):
        p=plan(field_request());self.assertEqual(p['request']['pressure_level'],['850']);self.assertNotIn('level_location',p['request'])
    def test_merra_pressure_instant(self):
        r=field_request('merra2');self.assertEqual(plan(r)['dataset'],'M2I3NPASM');self.assertEqual(r['temporal'],'instant')
    def test_merra_surface_mean(self):
        r=field_request('merra2','mslp',level=None,time='2024-01-01T00:30:00Z')
        self.assertEqual(plan(r)['dataset'],'M2T1NXSLV');self.assertEqual(r['temporal'],'mean_1h')
    def test_no_silent_rounding(self):
        for source,field,t in [('carra2','t','2024-01-01T01:00:00Z'),('merra2','mslp','2024-01-01T00:00:00Z'),('era5','t','2024-01-01T00:01:00Z')]:
            with self.subTest(source=source),self.assertRaises(ValueError):field_request(source,field,time=t)
    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):field_request(time='2024-01-01T00:00:00')
    def test_invalid_future_and_level(self):
        for kw in [dict(time='2099-01-01T00:00:00Z'),dict(level=True),dict(level=123),dict(area=[70,30,80,50]),dict(area=[80,170,70,-170])]:
            with self.subTest(kw=kw),self.assertRaises(ValueError):field_request(**kw)
    def test_no_invented_carra_vector(self):
        with self.assertRaises(ValueError):field_request('carra2','wind')
        self.assertIn('wind_speed',[f['id'] for s in catalog()['sources'] if s['id']=='carra2' for f in s['fields']])
    def test_hash_stable(self):self.assertEqual(identity(field_request()),identity(field_request()))


@unittest.skipUnless(HAS_FIELDS,'Optional field runtime is absent')
class NumericTests(unittest.TestCase):
    def test_kelvin_to_celsius_not_guess(self):
        ds,r=fixture();a,m=normalize(ds,r);np.testing.assert_allclose(a['value'],0,atol=1e-4);self.assertEqual(m['units'],'°C')
        ds['temperature'].attrs.pop('units')
        with self.assertRaises(ValueError):normalize(ds,r)
    def test_units(self):
        self.assertAlmostEqual(convert([98000],'Pa','pressure','SLP')[0],980)
        self.assertAlmostEqual(convert([49033.25],'m**2 s**-2','height','z')[0],5000)
        self.assertAlmostEqual(convert([.87],'1','humidity','RH')[0],87)
        for unit in ['DN','radiance','']:
            with self.assertRaises(ValueError):convert([273],unit,'temperature','T')
    def test_exact_time_level(self):
        ds,r=fixture()
        for other in [dict(r,time='2024-01-01T01:00:00Z'),dict(r,level=500)]:
            with self.assertRaises(ValueError):normalize(ds,other)
    def test_nodata_not_zero_or_filled(self):
        ds,r=fixture();ds.temperature.values[0,0,4,4]=np.nan;a,m=normalize(ds,r)
        v=sample(a,m,np.array([29.]),np.array([70.]))
        self.assertTrue(np.isnan(v['value'][0]));self.assertAlmostEqual(float(a['value'][0,0]),0,places=4)
    def test_outside_is_not_nearest_extrapolation(self):
        a,m=normalize(*fixture());v=sample(a,m,np.array([0.]),np.array([0.]));self.assertTrue(np.isnan(v['value'][0]))
    def test_pressure_pa(self):
        ds,r=fixture();ds=ds.assign_coords(level=[85000.]);ds.level.attrs['units']='Pa';a,m=normalize(ds,r);self.assertEqual(m['level'],850)
    def test_longitude_360(self):
        ds,r=fixture();ds=ds.assign_coords(lon=ds.lon.values+360);ds.lon.attrs['units']='degrees_east'
        a,m=normalize(ds,r);self.assertTrue(np.all((a['lon']>=28)&(a['lon']<=33)))
    def test_curvilinear(self):
        ds,r=fixture('carra2');vals=ds.temperature.values;lo,la=np.meshgrid(ds.lon.values,ds.lat.values)
        curved=xr.Dataset({'temperature':(('time','level','y','x'),vals)},coords={'time':ds.time,'level':ds.level,'lat':(('y','x'),la),'lon':(('y','x'),lo+.01*(la-70))})
        curved.temperature.attrs['units']='K';curved.lat.attrs['units']='degrees_north';curved.lon.attrs['units']='degrees_east'
        a,m=normalize(curved,r);self.assertFalse(m['regular']);self.assertEqual(m['source'],'carra2')
    def test_scalar_wind_magnitude_and_vector(self):
        ds,r=fixture(field='wind',value=3);ds['v_component_of_wind'][:]=4
        a,m=normalize(ds,r);np.testing.assert_allclose(a['value'],5);self.assertTrue(m['vector'])
        r=field_request(field='wind_speed');a,m=normalize(ds,r);np.testing.assert_allclose(a['value'],5);self.assertFalse(m['vector'])
    def test_grid_relative_vector_rejected(self):
        ds,r=fixture(field='wind');ds.u_component_of_wind.attrs['GRIB_uvRelativeToGrid']=1
        with self.assertRaises(ValueError):normalize(ds,r)
    def test_forecast_not_analysis(self):
        ds,r=fixture();ds=ds.assign_coords(step=np.timedelta64(3,'h'))
        with self.assertRaises(ValueError):normalize(ds,r)

    def test_multiple_expver_rejected(self):
        ds,r=fixture();ds=ds.expand_dims(expver=[1,5])
        with self.assertRaises(ValueError):normalize(ds,r)
    def test_temporal_averaging_rejected(self):
        ds,r=fixture();ds.temperature.attrs['cell_methods']='time: mean'
        with self.assertRaises(ValueError):normalize(ds,r)
    def test_radian_coordinates_rejected(self):
        ds,r=fixture();ds.lat.attrs['units']='radians'
        with self.assertRaises(ValueError):normalize(ds,r)
    def test_save_reopen_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            ds,r=fixture();a,m=normalize(ds,r);m.update(id='a'*24,created_at='2024-01-01T00:00:00Z');folder=Path(tmp)/m['id']
            saved=save(folder,a,m);aa,mm=load(tmp,m['id']);np.testing.assert_array_equal(aa['value'],a['value'])
            with xr.open_dataset(folder/'field.nc') as exported:self.assertIn('provenance',exported.attrs);self.assertEqual(exported.value.attrs['units'],'degC')
            (folder/'field.npz').write_bytes(b'broken')
            with self.assertRaisesRegex(ValueError,'сумма'):load(tmp,m['id'])
    def test_difference_sign_coarse_and_units(self):
        a,am=normalize(*fixture(value=278.15));b,bm=normalize(*fixture('merra2',value=273.15))
        am.update(id='a'*24,sha256='a'*64);bm.update(id='b'*24,sha256='b'*64)
        c,cm=difference(a,am,b,bm);np.testing.assert_allclose(c['value'],5,atol=1e-4)
        self.assertEqual(cm['units'],'K');self.assertEqual(cm['provenance']['target_source'],'merra2')
        for k,v in [('time','2024-01-01T03:00:00Z'),('level',500),('temporal','mean_1h'),('field','rh')]:
            with self.subTest(key=k),self.assertRaises(ValueError):difference(a,am,b,dict(bm,**{k:v}))
    def test_difference_mask_intersection(self):
        a,am=normalize(*fixture(value=278.15));b,bm=normalize(*fixture(value=273.15));b['value'][4,4]=np.nan
        am.update(id='a'*24,sha256='a'*64);bm.update(id='b'*24,sha256='b'*64)
        c,cm=difference(a,am,b,bm);self.assertTrue(np.isnan(c['value'][4,4]))
    def test_render_and_reuse(self):
        a,m=normalize(*fixture());a['value'][:]=np.arange(a['value'].shape[1])[None,:];m.update(id='a'*24,sha256='a'*64)
        with tempfile.TemporaryDirectory() as tmp:
            r=render(a,m,'barents',256,{'min':0,'max':25,'step':5},tmp);self.assertTrue(r['contours']);self.assertGreater(r['valid_pixels'],0)
            self.assertEqual(r['id'],render(a,m,'barents',256,{'min':0,'max':25,'step':5},tmp)['id'])
    def test_invalid_style(self):
        _,m=normalize(*fixture())
        for opts in [{'opacity':2},{'min':5,'max':0},{'step':0},{'mode':'wind'},{'min':float('nan')},{'step':.000001}]:
            with self.subTest(opts=opts),self.assertRaises(ValueError):style(opts,m)
    def test_html_not_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'fake.nc';p.write_text('<html>login</html>')
            with self.assertRaises(ValueError):read_local(p,field_request())


class TransportTests(unittest.TestCase):
    def test_cmr_discovery(self):
        p={'items':[{'umm':{'GranuleUR':'real-catalog-id','RelatedUrls':[{'URL':'https://opendap.earthdata.nasa.gov/collections/test/granules/test.nc4.dmr.html','Description':'OPENDAP DATA'}]}}]}
        url,g=find_opendap(p);self.assertTrue(url.endswith('.nc4'));self.assertEqual(g,'real-catalog-id')
    def test_cmr_empty_not_fabricated(self):
        with self.assertRaises(ValueError):find_opendap({'items':[]})
    def test_bad_host_and_protocol(self):
        for url in ['https://example.org/x','http://opendap.earthdata.nasa.gov/x','https://user@opendap.earthdata.nasa.gov/x','https://opendap.earthdata.nasa.gov.evil.test/x']:
            with self.subTest(url=url),self.assertRaises(ValueError):nasa_url(url)
    def test_auth_scoped(self):
        import requests
        seen=[]
        def send(session,prepared,**kw):seen.append(dict(prepared.headers));return requests.Response()
        cred={'format':'netrc','login':'SYNTHETIC','password':'SYNTHETIC'}
        with patch('requests.Session.send',send):
            s=nasa_session(cred);s.send(requests.Request('GET','https://opendap.earthdata.nasa.gov/x').prepare());s.send(requests.Request('GET','https://urs.earthdata.nasa.gov/x').prepare())
        self.assertNotIn('Authorization',seen[0]);self.assertTrue(seen[1]['Authorization'].startswith('Basic '))


@unittest.skipUnless(HAS_FIELDS,'Optional runtime is absent')
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.app=Workstation(self.root/'state')
        self.runtime=patch('arktika.reanalysis.service.ensure_runtime',return_value={'python':sys.executable});self.runtime.start()
        ds,r=fixture();self.r=r;self.p=self.root/'synthetic-test-only.nc';ds.to_netcdf(self.p,engine='scipy')
    def tearDown(self):
        self.app.cancel.set();self.wait(False);self.app.close();self.runtime.stop();self.tmp.cleanup()
    def wait(self,ok=True):
        until=time.monotonic()+40
        while self.app.busy and time.monotonic()<until:time.sleep(.05)
        self.assertFalse(self.app.busy)
        if ok:self.assertEqual(self.app.reanalysis_state()['job']['status'],'done',self.app.reanalysis_state()['job'])
    def add(self):
        self.app.reanalysis_prepare(dict(self.r,path=str(self.p),preset='barents',width=256));self.wait();return self.app.reanalysis_state()['layers'][-1]
    def test_import_layer_point_cache_and_restart(self):
        layer=self.add();self.assertIn('barents:256',layer['views'])
        point=self.app.reanalysis_point({'lon':30,'lat':70})['fields'][0];self.assertAlmostEqual(point['value'],0,places=4)
        self.assertAlmostEqual(point['native_lat'],70)
        self.app.reanalysis_layers({'action':'update','id':layer['id'],'style':{'opacity':.3},'visible':False})
        self.assertFalse(self.app.reanalysis_point({'lon':30,'lat':70})['fields'])
        self.app.close();self.app=Workstation(self.root/'state');self.assertEqual(len(self.app.reanalysis_state()['layers']),1)
        self.app.reanalysis_layers({'action':'cached','field_id':layer['field_id'],'preset':'barents','width':256});self.wait();self.assertEqual(len(self.app.reanalysis_state()['layers']),2)
    def test_reproject_and_remove(self):
        layer=self.add();self.app.reanalysis_map({'preset':'arctic','width':256});self.wait();self.assertIn('arctic:256',self.app.reanalysis_state()['layers'][0]['views'])
        self.app.reanalysis_layers({'action':'remove','id':layer['id']});self.assertFalse(self.app.reanalysis_state()['layers']);self.assertEqual(len(self.app.reanalysis_state()['fields']),1)
    def test_remote_requires_credentials(self):
        with self.assertRaises(ValueError):self.app.reanalysis_prepare(self.r)
    def test_cancel_checks_identity(self):
        with self.assertRaises(ValueError):self.app.reanalysis_cancel({'id':'other'})
    def test_source_gdex_not_cds(self):
        self.app._era5_init();self.app._era5_credential={'provider':'gdex','format':'netrc','hosts':{}}
        rows={s['id']:s for s in self.app.source_state()['sources']};self.assertEqual(rows['carra2']['status'],'credentials_missing')
    def test_artifact_path_and_checksum(self):
        layer=self.add()
        for kind,ident,name in [('field','../x','field.nc'),('render',layer['field_id'],'../../x'),('bad',layer['field_id'],'field.json')]:
            with self.assertRaises(ValueError):self.app.reanalysis_artifact(kind,ident,name)
        p=self.app.reanalysis_artifact('field',layer['field_id'],'field.nc');p.write_bytes(b'bad')
        with self.assertRaises(ValueError):self.app.reanalysis_artifact('field',layer['field_id'],'field.nc')

if __name__=='__main__':unittest.main()
