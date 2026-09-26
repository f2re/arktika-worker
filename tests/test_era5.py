"""Синтетические программные проверки. Не наблюдения и не верификация RTTOV."""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from arktika.era5_access import credentials_from_text,public_credentials,plan_requests,validate_netcdf,retrieve_plan
from arktika.era5_forward import spectral_passport,simulate_profiles
from arktika.era5_sync import fit_reference,normalize_dataset,load_group,interpolate_time,collocate,UNITS
from arktika.processing import apply_scale,validate_calibration
from arktika.workstation import Workstation
from arktika.download import digest,atomic_json
from arktika.network import Cancelled
from test_science import fixture

NOW=dt.datetime(2026,9,26,tzinfo=dt.timezone.utc)
HAS_XARRAY=bool(importlib.util.find_spec('xarray'))


class Credentials(unittest.TestCase):
    def test_cds_token(self):
        r=credentials_from_text('SYNTHETIC-SECRET','token');self.assertEqual(r['provider'],'cds')
        self.assertNotIn('SECRET',json.dumps(public_credentials(r)))
    def test_cdsapirc(self):
        r=credentials_from_text('url: https://cds.climate.copernicus.eu/api\nkey: SYNTHETIC-SECRET\n')
        self.assertEqual(r['format'],'cdsapirc')
    def test_netrc_cds(self):
        r=credentials_from_text('machine cds.climate.copernicus.eu login token password SYNTHETIC-SECRET')
        self.assertEqual(r['token'],'SYNTHETIC-SECRET')
    def test_netrc_ncar(self):
        r=credentials_from_text('machine rda.ucar.edu login "user@example.invalid" password "SYNTHETIC SECRET"')
        self.assertEqual(r['provider'],'gdex');self.assertNotIn('token',r)
    def test_default_and_macros_rejected(self):
        for text in ('default login x password y','macdef x\n echo SECRET'):
            with self.assertRaises(ValueError): credentials_from_text(text)
    def test_duplicates_rejected(self):
        with self.assertRaises(ValueError): credentials_from_text('machine rda.ucar.edu login x password y machine rda.ucar.edu login x password z')
    def test_wrong_host(self):
        with self.assertRaises(ValueError): credentials_from_text('machine urs.earthdata.nasa.gov login x password y')
    def test_unknown_hosts_not_retained(self):
        r=credentials_from_text('machine unrelated.invalid login x password UNUSED machine rda.ucar.edu login a password b')
        self.assertNotIn('unrelated.invalid',r['hosts'])
    def test_error_never_quotes_secret(self):
        for text in ('machine rda.ucar.edu login SECRET password', 'SECRET', 'url: https://bad.invalid\nkey: SECRET'):
            with self.assertRaises(ValueError) as e:credentials_from_text(text)
            self.assertNotIn('SECRET',str(e.exception))
    def test_size_limit(self):
        with self.assertRaises(ValueError): credentials_from_text('x'*65537)
    def test_mixed_credentials_not_merged(self):
        with self.assertRaises(ValueError): credentials_from_text('machine rda.ucar.edu login a password b machine cds.climate.copernicus.eu login a password b')


class Planning(unittest.TestCase):
    def plan(self,time='2024-09-20T23:30:00Z',ch=None,area=None):
        return plan_requests(time,ch or [9,10],area or [80,0,60,40],now=NOW)
    def test_midnight_separate_days(self):
        p=self.plan();self.assertEqual(p['times'],['2024-09-20T23:00:00Z','2024-09-21T00:00:00Z'])
        self.assertEqual(len(p['requests']),4);self.assertEqual(p['time_weight'],.5)
    def test_exact_hour_not_extra(self):self.assertEqual(len(self.plan('2024-09-20T23:00:00Z')['requests']),2)
    def test_dateline(self):
        p=self.plan(area=[80,170,70,-170]);self.assertEqual(len(p['requests']),8)
        self.assertEqual(p['requests'][0]['request']['area'],[80,170,70,180])
    def test_ozone_and_all_levels(self):
        p=self.plan();self.assertIn('ozone_mass_mixing_ratio',p['requests'][0]['request']['variable'])
        self.assertEqual(len(p['requests'][0]['request']['pressure_level']),37)
    def test_visible_rgb_blocked(self):
        for ch in ([1],[True],['rgb'],[]):
            with self.assertRaises(ValueError):plan_requests('2024-01-01T00:00Z',ch,[80,0,60,40],now=NOW)
    def test_future_naive_and_global_blocked(self):
        for time_,area in [('2027-01-01T00:00Z',[80,0,60,40]),('2024-01-01T00:00',[80,0,60,40]),('2024-01-01T00:00Z',[90,-180,-90,180])]:
            with self.assertRaises(ValueError):self.plan(time_,area=area)
    def test_malformed_area(self):
        for area in ([80,None,60,40],[80,float('nan'),60,40],[80,.1,60,40],[True,0,60,40]):
            with self.assertRaises(ValueError):self.plan(area=area)
    def test_recent_data_not_switched_to_another_day(self):
        p=self.plan('2026-09-25T23:30Z');self.assertTrue(p['likely_not_available']);self.assertIn('2026-09-26',p['times'][1])
    def test_channel4_night_requirement(self):self.assertTrue(any('ночные' in n for n in self.plan(ch=[4])['notes']))
    def test_plan_contains_no_credentials(self):self.assertNotIn('token',json.dumps(self.plan()))


class ReferenceFit(unittest.TestCase):
    def series(self):
        x=np.linspace(100,500,80);y=.2*x+200;g=[str(i//10) for i in range(80)];return x,y,g
    def test_known_affine(self):
        r=fit_reference(*self.series());self.assertAlmostEqual(r['scale'],.2);self.assertAlmostEqual(r['offset'],200)
        self.assertTrue(r['enforce_valid_dn']);self.assertEqual(r['status'],'assumed')
    def test_negative_slope_allowed(self):
        x,y,g=self.series();r=fit_reference(x,600-y,g);self.assertLess(r['scale'],0)
    def test_narrow_ocean_not_calibration_of_cold_clouds(self):
        x,y,g=self.series()
        with self.assertRaises(ValueError):fit_reference(x,y*.01+270,g)
    def test_spatial_groups_required(self):
        x,y,g=self.series()
        with self.assertRaises(ValueError):fit_reference(x,y,['one']*len(x))
    def test_bad_fit_rejected(self):
        x,y,g=self.series();y=y+np.random.default_rng(4).normal(0,10,len(y))
        with self.assertRaises(ValueError):fit_reference(x,y,g)
    def test_nonfinite(self):
        x,y,g=self.series();x[3]=np.nan
        with self.assertRaises(ValueError):fit_reference(x,y,g)
    def test_range_same_for_scalar_and_raster(self):
        p=fit_reference(*self.series());config={'mode':'custom','channels':{'9':p}};validate_calibration(config)
        a=apply_scale([50,100,300,500,510],9,config,.2,200)
        self.assertTrue(np.isnan(a[0]));self.assertTrue(np.isnan(a[-1]));self.assertEqual(a[2],260)
        self.assertTrue(np.isnan(apply_scale(510,9,config,.2,200)))
    def test_invalid_range_rejected(self):
        with self.assertRaises(ValueError):validate_calibration({'mode':'custom','channels':{'9':dict(scale=1,offset=0,units='K',status='assumed',enforce_valid_dn=True,valid_dn=[3,2])}})
    def test_srf_real_data_not_gain(self):
        p=spectral_passport();self.assertEqual(len(p['channels']),7);self.assertEqual(len(p['channels']['9']['samples']),23)
        self.assertEqual(p['channels']['9']['samples'][10],[952.381,1.]);self.assertNotIn('scale',p)
        for c in p['channels'].values():self.assertTrue(np.all(np.diff(np.array(c['samples'])[:,0])>0))


def model_files(folder,plan):
    """SYNTHETIC TEST ONLY: реальный NetCDF, искусственные атмосфера и поверхность."""
    import xarray as xr
    files=[];lat=np.arange(62.,74.,2.);lon=np.arange(2.,18.,2.);levels=np.array(plan['requests'][0]['request']['pressure_level'],float)
    shape=(1,len(levels),len(lat),len(lon));surface_shape=(1,len(lat),len(lon))
    for i,item in enumerate(plan['requests']):
        time_=np.datetime64(item['time'].replace('Z',''))
        coords={'time':[time_],'latitude':lat,'longitude':lon}
        dims=('time','latitude','longitude');data={}
        if item['group']=='pressure':
            coords['level']=levels;dims=('time','level','latitude','longitude')
            for key,value in [('t',260.),('q',.003),('o3',1e-6),('z',10000.)]:data[key]=(dims,np.full(shape,value),{'units':sorted(UNITS[key])[0]})
        else:
            for key,value in [('skt',280.),('sp',101325.),('t2m',279.),('d2m',275.),('u10',4.),('v10',2.),('tcc',0.),('siconc',0.),('lsm',0.)]:
                a=np.full(surface_shape,value)
                if key=='skt':a[0]+=((lat[:,None]-62)*1.5)+(lon[None,:]-2)*.25+(i//2)*2
                data[key]=(dims,a,{'units':sorted(UNITS[key])[0]})
        ds=xr.Dataset(data,coords=coords,attrs={'source':'SYNTHETIC TEST ONLY, NOT OBSERVATIONS'})
        if item['group']=='pressure':ds.level.attrs['units']='hPa'
        path=Path(folder)/('synthetic-%d.nc'%i);ds.to_netcdf(path,engine='netcdf4' if importlib.util.find_spec('netCDF4') else 'scipy')
        files.append({'path':str(path),'group':item['group'],'time':item['time'],'area':item['request']['area'],'sha256':digest(path)})
    return files


@unittest.skipUnless(HAS_XARRAY,'Для дополнительных тестов нужен xarray')
class ModelData(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.plan=plan_requests('2024-01-01T00:30:00Z',[9,10],[80,0,60,40],now=NOW)
        self.files=model_files(self.root,self.plan)
    def tearDown(self):self.tmp.cleanup()
    def test_magic_and_model_format(self):
        for f in self.files:validate_netcdf(f['path'])
    def test_html_not_netcdf(self):
        p=self.root/'bad.nc';p.write_text('<html>NOT DATA</html>')
        with self.assertRaises(ValueError):validate_netcdf(p)
    def test_interpolation_time(self):
        ds=load_group(self.files,'surface',self.plan)
        try:self.assertAlmostEqual(float(interpolate_time(ds,self.plan).skt.sel(latitude=62,longitude=2)),281.)
        finally:ds.close()
    def test_wrong_hour_rejected(self):
        self.files[0]['time']='2024-01-01T02:00:00Z'
        with self.assertRaises(ValueError):load_group(self.files,'pressure',self.plan)
    def test_wrong_units_rejected(self):
        import xarray as xr
        with xr.open_dataset(self.files[0]['path']) as ds:
            ds.t.attrs['units']='C'
            with self.assertRaises(ValueError):normalize_dataset(ds,'pressure')
    def test_profiles_and_raw_pixels(self):
        scene=fixture(self.root)
        scene['time']=self.plan['time']
        # Existing fixture covers lon20..50, outside these synthetic ERA5 nodes.
        r=collocate(scene,self.plan,self.files,20)
        self.assertEqual(r['profiles'],[]);self.assertGreater(r['rejected']['satellite_missing_or_texture'],0)
    def test_cancel_no_work(self):
        event=threading.Event();event.set()
        with self.assertRaises(Cancelled):retrieve_plan(self.plan,{},self.root/'cache',event)
    def test_cache_uses_sha(self):
        with patch('arktika.era5_access.run_worker') as worker:
            def fake(payload,*args):
                source=next(f['path'] for f in self.files if f['time']==payload['item']['time'] and f['group']==payload['item']['group'])
                Path(payload['target']).write_bytes(Path(source).read_bytes())
            worker.side_effect=fake
            one=retrieve_plan(self.plan,{},self.root/'cache');self.assertEqual(worker.call_count,4)
            retrieve_plan(self.plan,{},self.root/'cache');self.assertEqual(worker.call_count,4)
            Path(one[0]['path']).write_bytes(b'CORRUPTED')
            retrieve_plan(self.plan,{},self.root/'cache');self.assertEqual(worker.call_count,5)
            self.assertNotIn('credential', ''.join(p.read_text() for p in (self.root/'cache').glob('*.json')))


class Application(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);fixture(self.root)
        self.app=Workstation(self.root/'state');self.app.scan_local(self.root);self.scene=self.app.scenes()[0]['id']
    def tearDown(self):self.app.close();self.tmp.cleanup()
    def test_credentials_not_persisted(self):
        r=self.app.era5_credentials({'text':'SYNTHETIC-PRIVATE-TOKEN','format':'token'})
        self.assertEqual(r['storage'],'memory');state=self.app.era5_state()
        self.assertNotIn('PRIVATE-TOKEN',json.dumps(state));self.assertNotIn('PRIVATE-TOKEN',json.dumps(self.app.state()))
        self.app.era5_credentials({'clear':True});self.assertFalse(self.app.era5_state()['credentials']['present'])
    def test_inappropriate_channels_rejected(self):
        with self.assertRaises(ValueError):self.app.era5_plan({'scene':self.scene,'channels':[1]})
    def test_invalid_report_id(self):
        with self.assertRaises(ValueError):self.app.era5_report('../../token')
    def test_engine_not_replaced_by_surface_temperature(self):
        with patch('arktika.auto_calibration.readiness',return_value={'data_dependencies_missing':[],'pyrttov':False}):
            with self.assertRaisesRegex(ValueError,'RTTOV'):self.app.era5_start({'scene':self.scene,'channels':[9],'acknowledged':True})
    def test_apply_wrong_scene(self):
        with self.assertRaises(ValueError):self.app.era5_apply({'scene':self.scene,'id':'bad','channels':[9],'acknowledged':True})
    def test_apply_immutable_report_then_changed_file(self):
        report_id='a'*32;scene=self.app.scene(self.scene);path=Path(scene['channels']['9']['path'])
        proposal=fit_reference(np.linspace(100,500,80),np.linspace(220,300,80),[str(i//10) for i in range(80)])
        proposal['source_sha256']=digest(path)
        folder=self.app.store.root/'era5'/'runs'/report_id;folder.mkdir(parents=True)
        atomic_json(folder/'report.json',{'id':report_id,'scene':self.scene,'status':'ready','channels':{'9':{'status':'passed','proposal':proposal}}})
        self.app.era5_apply({'id':report_id,'scene':self.scene,'channels':[9],'acknowledged':True})
        self.assertEqual(self.app.radiometry_config(scene)[0]['channels']['9']['method'],'era5_electrol_proxy')
        with rasterio.open(path,'r+') as ds:ds.update_tags(changed='SYNTHETIC TEST')
        with self.assertRaisesRegex(ValueError,'изменился'):self.app.era5_apply({'id':report_id,'scene':self.scene,'channels':[9],'acknowledged':True})
        self.assertIn('9',self.app.radiometry_config(scene)[1])


class WorkerProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        path=Path(__file__).resolve().parents[1]/'scripts/era5_worker.py'
        spec=importlib.util.spec_from_file_location('era5_worker_test',path);cls.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.m)
    def test_credential_redirect_allowlist(self):
        for url in ('http://cds.climate.copernicus.eu/api','https://evil.invalid/file','https://rda.ucar.edu.evil.invalid/','https://rda.ucar.edu:8443/file'):
            self.assertFalse(self.m.allowed_url(url))
        self.assertTrue(self.m.allowed_url('https://tds.gdex.ucar.edu/thredds/catalog/file.xml'))
    def test_catalog_service_not_guessed(self):
        xml=b'<catalog><service serviceType="NetcdfSubset" base="/thredds/ncss/grid/"/><dataset urlPath="files/g/d633000/e5.oper.an.pl/202401/e5.oper.an.pl.128_130_t.ll025sc.2024010100_2024010123.nc"/></catalog>'
        service,path=self.m.gdex_catalog_selection(xml,'130','2024010103');self.assertEqual(service,'/thredds/ncss/grid/')
        with self.assertRaises(self.m.TransferError):self.m.gdex_catalog_selection(xml,'130','2024010203')
    def test_xml_entities_rejected(self):
        with self.assertRaises(self.m.TransferError):self.m.gdex_catalog_selection(b'<!DOCTYPE x SYSTEM "file:///etc/passwd">','130','2024')


@unittest.skipUnless(HAS_XARRAY,'Для дополнительных тестов нужен xarray')
class EndToEnd(unittest.TestCase):
    """RTTOV и сеть заменены явными программными фикстурами, остальные этапы настоящие."""
    def test_sync_fit_report_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'data';folder.mkdir()
            # Искусственные спутниковые DN в большой области; геопривязка настоящая.
            h,w=240,320;lat=80-(np.arange(h)+.5)*30/h;lon=(np.arange(w)+.5)*40/w
            bt=280+(lat[:,None]-62)*1.5+(lon[None,:]-2)*.25 # reference at 00:30 minus 1 K
            for ch in (9,10):
                with rasterio.open(folder/('A2_20240101003000_ch%02d.tif'%ch),'w',driver='GTiff',count=1,height=h,width=w,dtype='float32',crs=4326,transform=from_bounds(0,50,40,80,w,h)) as ds:
                    ds.write((2*bt-200).astype('float32'),1);ds.update_tags(source='SYNTHETIC TEST ONLY')
            app=Workstation(root/'state');app.scan_local(folder);scene=app.scenes()[0]['id']
            app.era5_credentials({'text':'SYNTHETIC-TOKEN','format':'token'})
            request={'scene':scene,'channels':[9,10],'area':[80,0,60,40],'provider':'cds','acknowledged':True,
                     'constant_angle_acknowledged':True,'zenith_deg':20,'allow_coefficient_download':True}
            def fake_retrieve(plan,credential,cache,event,progress):return model_files(folder,plan)
            def fake_forward(profiles,channels,*args):return np.array([[p['skin_k']-1]*len(channels) for p in profiles])
            ready={'data_dependencies_missing':[],'pyrttov':True,'coefficient':{'present':True}}
            try:
                with patch('arktika.auto_calibration.readiness',return_value=ready),patch('arktika.auto_calibration.retrieve_plan',side_effect=fake_retrieve),patch('arktika.auto_calibration.forward_isolated',side_effect=fake_forward),patch('arktika.auto_calibration.download_coefficients',return_value={'present':True,'sha256':'SYNTHETIC COEFFICIENT'}):
                    result=app.era5_start(request);app.task.join(10)
                    self.assertFalse(app.busy)
                    report=app.era5_report(result['id']);self.assertEqual(report['status'],'ready',report)
                    self.assertFalse(report['scientific_validation']);self.assertEqual(report['n_references'],48)
                    self.assertAlmostEqual(report['channels']['9']['proposal']['scale'],.5,places=4)
                    app.era5_apply({'id':result['id'],'scene':scene,'channels':[9,10],'acknowledged':True})
                    self.assertEqual(app.radiometry_config(app.scene(scene))[0]['channels']['9']['status'],'assumed')
                    text=json.dumps(report);self.assertNotIn('SYNTHETIC-TOKEN',text);self.assertNotIn(tmp,text)
            finally:app.close()

    def test_data_only_checks_returned_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fixture(root);app=Workstation(root/'state');app.scan_local(root)
            scene=app.scenes()[0]['id'];app.era5_credentials({'text':'SYNTHETIC-TOKEN','format':'token'})
            def wrong_time(plan,*args):
                files=model_files(root,plan);files[0]['time']='2026-01-01T05:00:00Z';return files
            try:
                with patch('arktika.auto_calibration.readiness',return_value={'data_dependencies_missing':[],'pyrttov':False}),patch('arktika.auto_calibration.retrieve_plan',side_effect=wrong_time):
                    result=app.era5_start({'scene':scene,'channels':[9],'data_only':True});app.task.join(10)
                    report=app.era5_report(result['id']);self.assertEqual(report['status'],'error')
                    self.assertIn('час',report['message']);self.assertEqual(report['channels'],{})
            finally:app.close()

class RTTOVContract(unittest.TestCase):
    def test_wrapper_arrays_and_channel_order(self):
        import types
        captured={}
        class Profiles:
            def __init__(self,n,m):self.n=n;self.m=m
        class Rttov:
            def __init__(self):self.Options=types.SimpleNamespace()
            def loadInst(self,channels):captured['channels']=channels
            def runDirect(self):
                captured['profiles']=self.Profiles;captured['emis_shape']=self.SurfEmisRefl.shape
                self.BtRefl=np.array([[275.,272.]])
            def dropInst(self):captured['closed']=True
        # This is an API-contract double, not a radiative-transfer calculation.
        profile={'pressure_hpa':[1,100,900],'temperature_k':[210,230,275],'humidity_kg_kg':[1e-6,.001,.005],
                 'ozone_kg_kg':[1e-6]*3,'zenith_deg':20,'sun_zenith_deg':110,'lat':70,'lon':20,
                 'skin_k':280,'sp_pa':101325,'t2m_k':279,'q2m_kg_kg':.004,'u10':4,'v10':2,'datetime':[2024,1,1,0,30,0]}
        with patch.dict('sys.modules',{'pyrttov':types.SimpleNamespace(Rttov=Rttov,Profiles=Profiles)}):
            r=simulate_profiles([profile],[9,10],'SYNTHETIC.dat')
        self.assertEqual(captured['profiles'].GasUnits,1);self.assertEqual(captured['emis_shape'],(5,1,2))
        self.assertEqual(captured['channels'],[9,10]);self.assertTrue(captured['closed']);self.assertEqual(r.shape,(1,2))


class HTTPBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server import LocalServer
        import urllib.request
        cls.tmp=tempfile.TemporaryDirectory();root=Path(cls.tmp.name);fixture(root)
        cls.app=Workstation(root/'state');cls.app.scan_local(root)
        cls.server=LocalServer(('127.0.0.1',0),cls.app,key='SYNTHETIC-LOCAL-SESSION')
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)
        cls.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.thread.join(3);cls.server.server_close();cls.app.close();cls.tmp.cleanup()
    def request(self,path,data=None,auth=True,origin=None):
        import urllib.request
        headers={'Cookie':'arktika_session=SYNTHETIC-LOCAL-SESSION'} if auth else {}
        if origin:headers['Origin']=origin
        if data is not None:headers.update({'Content-Type':'application/json','X-Arktika-Request':'1'})
        req=urllib.request.Request(self.base+path,data=None if data is None else json.dumps(data).encode(),headers=headers)
        with self.opener.open(req,timeout=5) as response:return response.status,response.headers,response.read()
    def test_private_status_requires_session(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/api/era5/state',auth=False)
        self.assertEqual(cm.exception.code,401)
    def test_cross_origin_credentials_rejected(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/api/era5/credentials',{'text':'SYNTHETIC-TOKEN','format':'token'},origin='https://other.invalid')
        self.assertEqual(cm.exception.code,403)
    def test_credentials_response_and_errors_no_secret(self):
        import urllib.error
        body=self.request('/api/era5/credentials',{'text':'SYNTHETIC-TOKEN','format':'token'})[2]
        self.assertNotIn(b'SYNTHETIC-TOKEN',body)
        self.assertNotIn(b'SYNTHETIC-TOKEN',self.request('/api/era5/state')[2])
        with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/api/era5/credentials',{'text':'PASSWORD-SECRET login','format':'auto'})
        self.assertEqual(cm.exception.code,400);self.assertNotIn(b'PASSWORD-SECRET',cm.exception.read())
    def test_static_js_and_html_not_md(self):
        status,headers,body=self.request('/era5.js');self.assertEqual(status,200);self.assertIn('javascript',headers['Content-Type'])
        status,headers,body=self.request('/docs/ERA5.html');self.assertEqual(status,200);self.assertIn('text/html',headers['Content-Type'])
    def test_planning_request_has_no_secrets(self):
        scene=self.app.scenes()[0]['id']
        status,headers,body=self.request('/api/era5/plan',{'scene':scene,'channels':[9,10]})
        self.assertEqual(status,200);self.assertNotIn(b'SYNTHETIC-TOKEN',body)

if __name__=='__main__':unittest.main()
