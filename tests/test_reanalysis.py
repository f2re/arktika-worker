"""Software fixtures only: synthetic fields test contracts, not meteorological accuracy."""
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile
import numpy as np
try:
    import xarray as xr
    import scipy
    import contourpy
    HAS_FIELDS=True
except ImportError:
    HAS_FIELDS=False
from arktika.reanalysis.catalog import catalogue, request_plan, validate_area, stamp
from arktika.reanalysis.transport import allowed_url, safe_session, validate_cds_schema, cmr_candidates, data_files, AccessError
from arktika.workstation import Workstation


def plan(source='era5',variable='t',**kwargs):
    return request_plan(dict(source=source,variable=variable,time='2024-01-01T00:00:00Z',
                             level=850 if variable in ('t','q','rh','z','wind','omega') else None,
                             area=[75,20,65,40],**kwargs))


def dataset(source='era5',variable='t',offset=0):
    """SYNTHETIC NetCDF variable with independently known unit conversion."""
    lats=np.arange(65.,76.);lons=np.arange(20.,41.)
    a=np.add.outer(lats-70,lons-30)/4+offset
    surface=variable in ('mslp','t2m','wind10','sst','ice')
    dims=('time','latitude','longitude') if surface else ('time','pressure_level','latitude','longitude')
    values=(a[None,:,:] if surface else a[None,None,:,:]).astype('float32')
    names={'era5':{'t':'t','q':'q','rh':'r','z':'z','wind':'u','omega':'w','mslp':'msl','t2m':'t2m','wind10':'u10','sst':'sst','ice':'ci'},
           'carra2':{'t':'temperature','wind':'wind_speed','wind10':'10m_wind_speed'},
           'merra2':{'t':'T','q':'QV','z':'H','wind':'U','omega':'OMEGA','mslp':'SLP','t2m':'T2M','wind10':'U10M'}}
    name=names[source][variable]
    unit={'t':'K','q':'kg kg-1','rh':'%','z':'m2 s-2','wind':'m s-1','omega':'Pa s-1','mslp':'Pa','t2m':'K','wind10':'m s-1','sst':'K','ice':'1'}[variable]
    if variable in ('t','t2m','sst'): values+=273.15
    if variable=='mslp': values=100000+values*100
    if variable in ('wind','wind10'): values[:]=10+offset
    if variable=='q': values[:]=.002
    if variable=='z': values[:]=1000*9.80665
    coords={'time':np.array(['2024-01-01T00:00:00'],dtype='datetime64[ns]'),
            'latitude':('latitude',lats,{'units':'degrees_north'}),
            'longitude':('longitude',lons,{'units':'degrees_east'})}
    if not surface: coords['pressure_level']=('pressure_level',[850.],{'units':'hPa'})
    data={name:(dims,values,{'units':unit})}
    if variable in ('wind','wind10'):
        second={'era5':'v' if variable=='wind' else 'v10','merra2':'V' if variable=='wind' else 'V10M','carra2':'wind_direction' if variable=='wind' else '10m_wind_direction'}[source]
        data[second]=(dims,np.full_like(values,270 if source=='carra2' else 0),{'units':'degrees' if source=='carra2' else 'm s-1','standard_name':'wind_from_direction' if source=='carra2' else 'northward_wind'})
    return xr.Dataset(data,coords=coords,attrs={'title':'SYNTHETIC SOFTWARE TEST, NOT OBSERVATIONS'})


class RequestPlans(unittest.TestCase):
    def test_catalogue_has_only_supported_fields(self):
        rows={s['id']:s for s in catalogue()['sources']}
        self.assertEqual(set(rows),{'era5','carra2','merra2'})
        self.assertNotIn('omega',[v['id'] for v in rows['carra2']['variables']])
        self.assertNotIn('rh',[v['id'] for v in rows['merra2']['variables']])
    def test_era5_pressure(self):
        p=plan();r=p['requests'][0]
        self.assertEqual(r['dataset'],'reanalysis-era5-pressure-levels')
        self.assertEqual(r['request']['pressure_level'],['850'])
    def test_carra_real_schema_keys(self):
        r=plan('carra2')['requests'][0]['request']
        self.assertEqual(r['level_location'],['850']);self.assertEqual(r['product_type'],'analysis')
        self.assertEqual(r['level_type'],'pressure_levels');self.assertNotIn('pressure_level',r)
        self.assertNotIn('leadtime_hour',r)
    def test_surface_separate_collection(self):
        self.assertEqual(plan(variable='mslp')['requests'][0]['dataset'],'reanalysis-era5-single-levels')
    def test_merra_instantaneous_not_tavg(self):
        self.assertEqual(plan('merra2')['requests'][0]['dataset'],'M2I3NPASM')
        self.assertEqual(plan('merra2','t2m')['requests'][0]['dataset'],'M2I1NXASM')
    def test_wind_gets_both_components(self):
        r=plan(variable='wind')['requests'][0]['request']
        self.assertEqual(len(r['variable']),2)
    def test_timezone_explicit(self):
        with self.assertRaises(ValueError): stamp('2024-01-01T00:00:00')
        self.assertEqual(stamp('2024-01-01T03:00:00+03:00').hour,0)
    def test_time_not_rounded(self):
        with self.assertRaises(ValueError): stamp('2024-01-01T00:30:00Z')
        d=plan('carra2');d['time']='2024-01-01T01:00:00Z'
        with self.assertRaises(ValueError): request_plan(d)
    def test_surface_merra_hourly(self):
        d=plan('merra2','t2m');d['time']='2024-01-01T01:00:00Z';self.assertTrue(request_plan(d))
    def test_carra_domain_restriction(self):
        d=plan('carra2');d['area']=[50,0,20,40]
        with self.assertRaises(ValueError): request_plan(d)
    def test_level_not_guessed(self):
        d=plan();d['level']=851
        with self.assertRaises(ValueError): request_plan(d)
    def test_dateline_split(self):
        d=plan();d['area']=[80,170,60,-170]
        p=request_plan(d);self.assertEqual(len(p['requests']),2)
        self.assertEqual(p['requests'][0]['request']['area'],[80,170,60,180])
    def test_area_rejects_nonfinite(self):
        for a in ([75,20,65,float('nan')],[75,20,75,40],[True,20,65,40]):
            with self.subTest(a=a),self.assertRaises(ValueError): validate_area(a)
    def test_key_changes_with_source_time_and_level(self):
        d=plan();a=d['key'];d['time']='2024-01-01T03:00:00Z'
        self.assertNotEqual(a,request_plan(d)['key']);self.assertNotEqual(a,plan('carra2')['key'])
    def test_schema_publication_guard(self):
        schema={'inputs':{'year':{'schema':{'type':'array','items':{'enum':['2024','2025']}}}}}
        validate_cds_schema(schema,{'year':['2024']})
        with self.assertRaises(AccessError): validate_cds_schema(schema,{'year':['2026']})
    def test_schema_rejects_unknown_field(self):
        with self.assertRaises(AccessError): validate_cds_schema({'inputs':{'year':{}}},{'invented':0})


class NetworkContracts(unittest.TestCase):
    def test_allowlist(self):
        for url in ('http://cds.climate.copernicus.eu','https://cds.climate.copernicus.eu.evil.test/x','https://localhost/a','https://urs.earthdata.nasa.gov@evil.test/x'):
            self.assertFalse(allowed_url(url))
        self.assertTrue(allowed_url('https://cds.climate.copernicus.eu/api'))
    def recorded_headers(self,credential,urls):
        import requests
        from requests.adapters import BaseAdapter
        rows=[]
        class Recorder(BaseAdapter):
            def send(self,request,**kw):
                rows.append(dict(request.headers));r=requests.Response();r.status_code=200;r._content=b'{}';r.request=request;r.url=request.url;return r
            def close(self): pass
        with safe_session(credential) as s:
            s.mount('https://',Recorder())
            for url in urls: s.get(url)
        return rows
    def test_cds_secret_only_cds(self):
        rows=self.recorded_headers({'provider':'cds','token':'SYNTHETIC-PRIVATE'},['https://cds.climate.copernicus.eu/api','https://download.ecmwf.int/data','https://cmr.earthdata.nasa.gov/search'])
        self.assertEqual(rows[0].get('PRIVATE-TOKEN'),'SYNTHETIC-PRIVATE')
        self.assertNotIn('PRIVATE-TOKEN',rows[1]);self.assertNotIn('PRIVATE-TOKEN',rows[2])
    def test_nasa_basic_only_urs(self):
        rows=self.recorded_headers({'provider':'earthdata','format':'netrc','login':'test','password':'SYNTHETIC'},['https://urs.earthdata.nasa.gov/test','https://goldsmr5.gesdisc.eosdis.nasa.gov/test','https://cmr.earthdata.nasa.gov/search'])
        self.assertTrue(rows[0]['Authorization'].startswith('Basic '));self.assertNotIn('Authorization',rows[1]);self.assertNotIn('Authorization',rows[2])
    def test_nasa_token_not_cmr(self):
        rows=self.recorded_headers({'provider':'earthdata','format':'token','token':'SYNTHETIC'},['https://goldsmr5.gesdisc.eosdis.nasa.gov/test','https://cmr.earthdata.nasa.gov/search'])
        self.assertEqual(rows[0]['Authorization'],'Bearer SYNTHETIC');self.assertNotIn('Authorization',rows[1])
    def test_no_user_proxy_or_netrc(self):
        self.assertFalse(safe_session({}).trust_env)
    def test_cmr_filters_date_and_dataset(self):
        base='https://goldsmr5.gesdisc.eosdis.nasa.gov/opendap/MERRA2/M2I3NPASM.5.12.4/2024/01/'
        response={'feed':{'entry':[{'id':'G-test','links':[{'href':base+'MERRA2_400.inst3_3d_asm_Np.20240101.nc4.html'}, {'href':base+'MERRA2_400.inst3_3d_asm_Np.20240102.nc4.html'}, {'href':'https://evil.test/opendap/M2I3NPASM20240101.nc4'}]}]}}
        found=cmr_candidates(response,'M2I3NPASM','2024-01-01');self.assertEqual(len(found),1);self.assertTrue(found[0]['url'].endswith('.nc4'))
    def test_html_not_netcdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'response.nc';p.write_bytes(b'<html>login</html>')
            with self.assertRaises(AccessError): data_files(p,Path(tmp))
    def test_archive_generated_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'response.zip'
            with zipfile.ZipFile(p,'w') as z:z.writestr('../../escape.nc',b'CDF\x01dummy')
            found=data_files(p,root);self.assertEqual(found[0].parent,root);self.assertEqual(found[0].name,'part-0.nc')
    def test_archive_count_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'response.zip'
            with zipfile.ZipFile(p,'w') as z:
                for i in range(17):z.writestr(str(i)+'.nc',b'CDF\x01dummy')
            with self.assertRaises(AccessError):data_files(p,root)


@unittest.skipUnless(HAS_FIELDS,'Optional field dependencies unavailable')
class NativeFields(unittest.TestCase):
    def normalize(self,ds=None,p=None):
        from arktika.reanalysis.normalize import normalize
        return normalize(dataset() if ds is None else ds,plan() if p is None else p)
    def test_kelvin_celsius(self):
        native=self.normalize();self.assertAlmostEqual(float(native.value[5,10]),0,places=4)
        self.assertEqual(native.attrs['unit'],'°C')
    def test_pa_hpa(self):
        d=self.normalize(dataset(variable='mslp'),plan(variable='mslp'));self.assertAlmostEqual(float(d.value[5,10]),1000,places=4)
    def test_geopotential_height(self):
        d=self.normalize(dataset(variable='z'),plan(variable='z'));self.assertAlmostEqual(float(d.value[5,10]),1000,places=3)
    def test_specific_humidity(self):
        d=self.normalize(dataset(variable='q'),plan(variable='q'));self.assertAlmostEqual(float(d.value[5,10]),2,places=5)
    def test_unknown_units_rejected(self):
        ds=dataset();ds.t.attrs['units']='DN'
        with self.assertRaises(ValueError):self.normalize(ds)
    def test_omega_not_geometric_speed(self):
        ds=dataset(variable='omega');ds.w.attrs['units']='m s-1'
        with self.assertRaises(ValueError):self.normalize(ds,plan(variable='omega'))
    def test_time_exact(self):
        ds=dataset().assign_coords(time=np.array(['2024-01-01T03:00'],dtype='datetime64[ns]'))
        with self.assertRaises(ValueError):self.normalize(ds)
    def test_duplicate_times_rejected(self):
        ds=xr.concat([dataset(),dataset()],dim='time')
        with self.assertRaises(ValueError):self.normalize(ds)
    def test_level_pressure_pa(self):
        ds=dataset().rename({'pressure_level':'plev'}).assign_coords(plev=('plev',[85000.],{'units':'Pa'}))
        self.assertTrue(np.isfinite(self.normalize(ds).value).any())
    def test_level_unknown_units(self):
        ds=dataset().rename({'pressure_level':'lev'});ds.lev.attrs={}
        with self.assertRaises(ValueError):self.normalize(ds)
    def test_forecast_rejected(self):
        ds=dataset().assign_coords(step=np.timedelta64(3,'h'))
        with self.assertRaises(ValueError):self.normalize(ds)
    def test_ensemble_not_silently_selected(self):
        ds=dataset().expand_dims(number=[0,1])
        with self.assertRaises(ValueError):self.normalize(ds)
    def test_nodata_hole_not_filled(self):
        from arktika.reanalysis.render import sampling
        ds=dataset();ds.t.values[0,0,5,10]=np.nan
        native=self.normalize(ds)
        self.assertTrue(np.isnan(sampling(native,30,70)['value']))
    def test_outside_coverage_not_nearest_filled(self):
        from arktika.reanalysis.render import sampling
        self.assertTrue(np.isnan(sampling(self.normalize(),0,0)['value']))
    def test_uv_to_speed(self):
        native=self.normalize(dataset(variable='wind'),plan(variable='wind'))
        self.assertAlmostEqual(float(native.value[5,10]),10)
        self.assertAlmostEqual(float(native.u[5,10]),10)
    def test_grid_wind_rejected(self):
        ds=dataset(variable='wind');ds.u.attrs['GRIB_uvRelativeToGrid']=1
        with self.assertRaises(ValueError):self.normalize(ds,plan(variable='wind'))
    def test_carra_west_wind_to_east(self):
        native=self.normalize(dataset('carra2','wind'),plan('carra2','wind'))
        self.assertAlmostEqual(float(native.u[5,10]),10,places=4)
        self.assertAlmostEqual(float(native.v[5,10]),0,places=4)
    def test_difference_sign_and_coarse_grid(self):
        from arktika.reanalysis.render import difference
        a=self.normalize(dataset('merra2',offset=2),plan('merra2'));b=self.normalize(dataset(offset=0))
        result=difference(a,b,'a','b');self.assertAlmostEqual(float(result.value[5,10]),2,places=4)
        self.assertEqual(result.attrs['resolution_km'],55.6);self.assertEqual(result.value.shape,a.value.shape)
    def test_difference_mismatched_time(self):
        from arktika.reanalysis.render import difference
        a=self.normalize();b=a.copy(deep=True);b.attrs['time']='2024-01-01T03:00:00Z'
        with self.assertRaises(ValueError):difference(a,b,'a','b')
    def test_render_has_png_contours_geometry(self):
        from arktika.reanalysis.render import render
        from arktika.geo import grid
        from rasterio.transform import from_bounds
        from PIL import Image
        g=grid('geographic',256);g.update(bounds=[20,65,40,75],width=256,height=128,transform=from_bounds(20,65,40,75,256,128))
        with tempfile.TemporaryDirectory() as tmp:
            result=render(self.normalize(),g,tmp,{'min':-3,'max':3})
            self.assertGreater(result['valid_pixels'],0);self.assertTrue(result['contours'])
            with Image.open(Path(tmp)/'map.png') as img:self.assertEqual(img.size,(256,128));self.assertEqual(img.mode,'RGBA')
    def test_curvilinear_grid(self):
        ds=dataset();lat,lon=np.meshgrid(ds.latitude.values,ds.longitude.values,indexing='ij')
        ds=ds.rename({'latitude':'j','longitude':'i'}).assign_coords(latitude=(('j','i'),lat),longitude=(('j','i'),lon))
        self.assertEqual(self.normalize(ds).value.shape,(11,21))
    def test_local_dateline_keeps_both_sides(self):
        ds=dataset().assign_coords(longitude=np.linspace(170,190,21));p=plan();p['area']=[75,170,65,-170]
        n=self.normalize(ds,p);self.assertGreaterEqual(n.sizes['x'],20)


@unittest.skipUnless(HAS_FIELDS,'Optional field dependencies unavailable')
class ServiceIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.app=Workstation(self.root/'state');self.raw=self.root/'synthetic.nc'
        dataset().to_netcdf(self.raw,engine='scipy')
    def tearDown(self):
        self.app.cancel.set()
        if self.app.task and self.app.task.is_alive():self.app.task.join(10)
        self.app.close();self.tmp.cleanup()
    def load(self,**extra):
        data=dict(plan(),path=str(self.raw),**extra);r=self.app.fields_add(data,local=True)
        self.app.task.join(20)
        self.assertFalse(self.app.busy)
        state=self.app.fields_state();self.assertEqual(state['job']['id'],r['id'])
        self.assertEqual(state['job']['status'],'done',state['job'])
        return state['job']['field_id']
    def test_real_subprocess_import_render_probe_and_export(self):
        identity=self.load();meta=self.app.field(identity)
        self.assertEqual(meta['time'],'2024-01-01T00:00:00Z')
        self.assertNotIn('token',json.dumps(meta));self.assertNotIn(str(self.root),json.dumps(meta))
        self.app.fields_stack({'stack':[{'id':identity,'style':'contours','opacity':.5}]})
        rendered=self.app.fields_render({'id':identity,'preset':'barents','width':256})
        self.assertGreater(rendered['valid_pixels'],0)
        point=self.app.fields_probe({'ids':[identity],'lon':30,'lat':70})
        self.assertAlmostEqual(point['rows'][0]['value'],0,places=4)
        out=self.app.fields_export(identity)
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            self.assertEqual(set(z.namelist()),{'field.nc','provenance.json'})
    def test_stack_limits_and_duplicates(self):
        identity=self.load()
        for rows in ([{'id':identity}]*2,[{'id':identity,'opacity':float('nan')}],[{'id':identity,'style':'arrows'}]):
            with self.subTest(rows=rows),self.assertRaises(ValueError):self.app.fields_stack({'stack':rows})
    def test_cache_reuse(self):
        a=self.load();b=self.load();self.assertEqual(a,b)
        self.assertIn('кэш',self.app.fields_state()['job']['message'])
    def test_modified_file_is_new_version(self):
        a=self.load();dataset(offset=2).to_netcdf(self.raw,engine='scipy');b=self.load();self.assertNotEqual(a,b)
    def test_tampered_native_rejected(self):
        identity=self.load();p=self.app.field_cache/identity/'field.nc';p.write_bytes(b'broken')
        with self.assertRaises(ValueError):self.app.field(identity)
    def test_no_credential_no_remote_call(self):
        with self.assertRaises(ValueError):self.app.fields_add(plan())
    def test_id_traversal(self):
        with self.assertRaises(ValueError):self.app.field('../../secret')
    def test_cds_does_not_accept_gdex(self):
        with self.assertRaises(ValueError):self.app.source_credentials({'provider':'cds','format':'netrc','text':'machine gdex.ucar.edu login user password secret'})
    def test_exact_cancel_id(self):
        with self.assertRaises(ValueError):self.app.fields_cancel({'id':'wrong'})


@unittest.skipUnless(HAS_FIELDS,'Optional field dependencies unavailable')
class AdditionalContracts(unittest.TestCase):
    def test_mean_time_not_analysis(self):
        from arktika.reanalysis.normalize import normalize
        ds=dataset();ds.t.attrs['cell_methods']='time: mean'
        with self.assertRaises(ValueError):normalize(ds,plan())
    def test_accumulation_not_analysis(self):
        from arktika.reanalysis.normalize import normalize
        ds=dataset();ds.t.attrs['GRIB_stepType']='accum'
        with self.assertRaises(ValueError):normalize(ds,plan())
    def test_surface_not_upper_air_temperature(self):
        from arktika.reanalysis.normalize import normalize
        ds=dataset();ds.t.attrs['standard_name']='air_temperature'
        with self.assertRaises(ValueError):normalize(ds,plan(variable='t2m'))
    def test_cf_coordinates_and_units_exported(self):
        from arktika.reanalysis.normalize import normalize,save_native
        ds=normalize(dataset(variable='q'),plan(variable='q'))
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'native.nc';save_native(ds,p)
            with xr.open_dataset(p) as saved:
                self.assertIn('latitude',saved.coords);self.assertIn('longitude',saved.coords)
                self.assertEqual(saved.value.attrs['units'],'g kg-1')
                self.assertEqual(saved.attrs['Conventions'],'CF-1.8')
    def test_native_roundtrip_temperature(self):
        from arktika.reanalysis.normalize import normalize,save_native
        native=normalize(dataset(),plan())
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'native.nc';save_native(native,p)
            with xr.open_dataset(p) as saved:
                result=normalize(saved,plan())
                np.testing.assert_allclose(result.value,native.value,atol=1e-5)
                self.assertIn('time',saved.coords);self.assertIn('pressure_level',saved.coords)
    def test_native_roundtrip_carra_wind(self):
        from arktika.reanalysis.normalize import normalize,save_native
        native=normalize(dataset('carra2','wind'),plan('carra2','wind'))
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'native.nc';save_native(native,p)
            with xr.open_dataset(p) as saved:
                result=normalize(saved,plan('carra2','wind'))
                np.testing.assert_allclose(result.u,native.u,atol=1e-5)
    def test_carra_grid_relative_direction_rejected(self):
        from arktika.reanalysis.normalize import normalize
        d=dataset('carra2','wind');d.wind_direction.attrs['GRIB_uvRelativeToGrid']=1
        with self.assertRaises(ValueError):normalize(d,plan('carra2','wind'))
    def test_calm_has_no_arrow(self):
        from arktika.reanalysis.normalize import normalize
        from arktika.reanalysis.render import render
        from arktika.geo import grid
        ds=dataset(variable='wind');ds.u.values[:]=0;ds.v.values[:]=0
        native=normalize(ds,plan(variable='wind'))
        with tempfile.TemporaryDirectory() as tmp:
            r=render(native,grid('barents',256),tmp);self.assertEqual(r['vectors'],[])
    def test_dateline_parts_keep_coordinate_arrays(self):
        from arktika.reanalysis.normalize import normalize
        from arktika.reanalysis.transport import combine_parts
        ds=dataset();a=normalize(ds,plan());b=normalize(ds,plan())
        merged=combine_parts([a,b]);self.assertIn('latitude',merged.coords)
        self.assertEqual(merged.value.size,2*a.value.size)
    def test_unusual_port_and_bad_url_rejected(self):
        self.assertFalse(allowed_url('https://cds.climate.copernicus.eu:1234/api'))
        self.assertFalse(allowed_url('https://cds.climate.copernicus.eu:bad/api'))
    def test_legacy_cds_clear_clears_unified_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=Workstation(Path(tmp)/'state')
            try:
                app.source_credentials({'provider':'cds','text':'SYNTHETIC-TOKEN','format':'token'})
                self.assertTrue(app.source_credential('carra2'))
                app.era5_credentials({'clear':True})
                self.assertFalse(app.source_credential('carra2'))
            finally:app.close()

@unittest.skipUnless(HAS_FIELDS,'Optional field dependencies unavailable')
class FieldHTTP(unittest.TestCase):
    def setUp(self):
        import threading
        import requests
        from server import LocalServer
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.app=Workstation(self.root/'state')
        self.raw=self.root/'synthetic.nc';dataset().to_netcdf(self.raw,engine='scipy')
        self.server=LocalServer(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.client=requests.Session();self.client.trust_env=False
        self.client.headers['X-Arktika-Request']='1'
    def tearDown(self):
        self.app.cancel.set()
        if self.app.task and self.app.task.is_alive():self.app.task.join(10)
        self.server.shutdown();self.server.server_close();self.client.close();self.app.close();self.tmp.cleanup()
    def post(self,path,data):return self.client.post(self.base+path,json=data,timeout=30)
    def login(self):self.assertEqual(self.post('/api/bootstrap',{'key':self.server.key}).status_code,200)
    def test_protected_and_cross_origin(self):
        self.assertEqual(self.client.get(self.base+'/api/reanalysis/state').status_code,401)
        self.login()
        self.assertEqual(self.client.get(self.base+'/api/reanalysis/state',headers={'Origin':'https://untrusted.invalid'}).status_code,403)
    def test_styles_and_scripts_served(self):
        for path,mime in [('/reanalysis.js','application/javascript'),('/reanalysis.css','text/css')]:
            r=self.client.get(self.base+path);self.assertEqual(r.status_code,200);self.assertTrue(r.headers['Content-Type'].startswith(mime))
            self.assertIn("script-src 'self'",r.headers['Content-Security-Policy'])
    def test_end_to_end_local_http(self):
        self.login();response=self.post('/api/reanalysis/import',dict(plan(),path=str(self.raw)))
        self.assertEqual(response.status_code,200,response.text)
        self.app.task.join(20)
        state=self.client.get(self.base+'/api/reanalysis/state').json()
        self.assertEqual(state['job']['status'],'done',state);identity=state['job']['field_id']
        rendered=self.post('/api/reanalysis/render',{'id':identity,'preset':'barents','width':256})
        self.assertEqual(rendered.status_code,200,rendered.text)
        r=self.client.get(self.base+rendered.json()['image']);self.assertTrue(r.content.startswith(b'\x89PNG'))
        sampled=self.post('/api/reanalysis/probe',{'ids':[identity],'lon':30,'lat':70});self.assertEqual(sampled.status_code,200,sampled.text)
        self.assertAlmostEqual(sampled.json()['rows'][0]['value'],0,places=4)
        r=self.client.get(self.base+'/field-export/'+identity);self.assertTrue(r.content.startswith(b'PK'))
        state_text=json.dumps(state);self.assertNotIn(str(self.root),state_text)
    def test_sources_state_does_not_echo_secret(self):
        self.login();secret='SYNTHETIC-HIDDEN-TOKEN'
        self.post('/api/sources/credentials',{'provider':'cds','text':secret,'format':'token'})
        r=self.client.get(self.base+'/api/sources');self.assertNotIn(secret,r.text)
        rows={x['id']:x for x in r.json()['sources']};self.assertEqual(rows['era5']['layer_status'],'fields_available')

if __name__=='__main__':unittest.main()
