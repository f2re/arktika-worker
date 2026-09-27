"""Orchestration/security regressions; SYNTHETIC SOFTWARE TESTS, not observations."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from arktika.auth import AuthClient
from arktika.download import atomic_json
from arktika.era5_runtime import ensure_runtime, python_path, run_process, configure_engine, probe
from arktika.era5_workflow import NeedAction
from arktika.model import normalize_item
from arktika.network import Cancelled
from arktika.workstation import Workstation
from test_archive import RGBTransport
from test_science import fixture
from test_era5 import HAS_XARRAY,model_files
from arktika.era5_workflow import run_calculation

RUNTIME={'python':sys.executable,'missing':[],'versions':{'test':'synthetic'},'pyrttov':False,'managed':False}


def workflow_fixture(root):
    """Only channel 9 initially local; real queue downloads test TIFFs 7 and 10."""
    original=root/'original';original.mkdir();scene=fixture(original)
    local=root/'local';local.mkdir()
    local.joinpath('A2_20260101000000_ch09.tif').write_bytes(original.joinpath('A2_20260101000000_ch09.tif').read_bytes())
    # Transport uses the same synthetic TIFF for both test channels, not scientific pairs.
    transport=RGBTransport(original.joinpath('A2_20260101000000_ch07.tif').read_bytes())
    app=Workstation(root/'state',download_dir=root/'downloads',client=AuthClient(transport=transport))
    app.scan_local(local)
    raw={'type':'Feature','id':'SYNTHETIC-WORKFLOW','properties':{'platform':'ARCM2','datetime':scene['time'],'processing:level':'L2IR'},'assets':{}}
    for c in (7,9,10):
        raw['assets'][str(c)]={'href':f'https://s3.gptl.ru/SYNTHETIC-WORKFLOW/A2_20260101000000_ch{c:02d}.tif','type':'image/tiff','proj:epsg':4326}
    record,assets=normalize_item(raw,'SYNTHETIC-WORKFLOW')
    app.store.upsert([record],assets)
    return app,scene,transport,assets


def fake_report(scene,plan,options,root,credential,identity,runtime,cancel,progress):
    """Public report fixture. No actual external ERA5 or RTTOV is claimed here."""
    progress('Проверяю ERA5: SYNTHETIC SOFTWARE TEST')
    report={'id':identity,'scene':scene['id'],'time':scene['time'],'platform':scene['platform'],
            'status':'data_ready','channels':{},'message':'SYNTHETIC TEST DATA READY',
            'era5_files':[{'group':r['group'],'time':r['time']} for r in plan['requests']], 'plan':plan,'scientific_validation':False}
    if options.get('prepare_coefficients'):report['coefficient']={'present':True,'sha256':'SYNTHETIC-TEST-ONLY'}
    folder=Path(root)/'era5'/'runs'/identity;folder.mkdir(parents=True,exist_ok=True)
    atomic_json(folder/'report.json',report)
    return report


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.app,self.scene,self.transport,self.assets=workflow_fixture(self.root)
        self.runtime=patch('arktika.era5_workflow.runtime_info',return_value=copy.deepcopy(RUNTIME));self.runtime.start()
        self.public_runtime=patch('arktika.auto_calibration.runtime_info',return_value=copy.deepcopy(RUNTIME));self.public_runtime.start()
        self.app.era5_credentials({'text':'SYNTHETIC-TOKEN-NOT-SECRET','format':'token'})
        self.body={'scene':self.scene['id'],'product':'micro24','channels':[9], 'acknowledged':True}
    def tearDown(self):
        self.app.cancel.set()
        until=time.monotonic()+5
        while self.app.busy and time.monotonic()<until:time.sleep(.05)
        self.app.close();self.runtime.stop();self.public_runtime.stop();self.temp.cleanup()
    def wait(self):
        until=time.monotonic()+8
        while self.app.busy and time.monotonic()<until:time.sleep(.02)
        self.assertFalse(self.app.busy)
        return self.app.era5_state()['job']
    def start(self,body=None,calculate=fake_report,runtime=None):
        with patch('arktika.era5_workflow.ensure_runtime',return_value=copy.deepcopy(runtime or RUNTIME)), patch('arktika.era5_workflow.run_calculation',side_effect=calculate) as run:
            self.app.era5_start(body or self.body);state=self.wait()
        return state,run
    def test_preflight_derives_all_channels_from_product_not_stale_ui(self):
        r=self.app.era5_preflight(self.body)
        self.assertEqual(r['plan']['channels'],[7,9,10]);self.assertEqual(r['inventory']['ready'],[9]);self.assertEqual(r['inventory']['missing'],[7,10])
        self.assertEqual(r['next_action'],'start');self.assertFalse(self.app.store.jobs())
    def test_data_plan_without_any_local_scene(self):
        body={'platform':'ARCM2','time':'2024-01-01T23:30:00Z','product':'night'}
        r=self.app.era5_plan(body)
        self.assertEqual(r['channels'],[4,9,10]);self.assertEqual(r['times'],['2024-01-01T23:00:00Z','2024-01-02T00:00:00Z'])
    def test_future_publication_gates_before_installs_and_channels(self):
        stamp=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=3)).isoformat()
        body={'platform':'ARCM2','time':stamp,'product':'micro24','acknowledged':True}
        p=self.app.era5_preflight(body);self.assertEqual(p['next_action'],'archive')
        with patch('arktika.era5_workflow.ensure_runtime') as install,patch('arktika.era5_workflow.run_calculation') as run:
            self.app.era5_start(body);job=self.wait()
        self.assertEqual(job['next_action'],'archive');self.assertFalse(install.called);self.assertFalse(run.called)
        self.assertFalse(self.app.store.jobs());self.assertEqual(job['time'],self.app.era5_plan(body)['time'])
    def test_access_before_install_no_raw_token_persisted(self):
        self.app.era5_credentials({'clear':True})
        state,run=self.start();self.assertEqual(state['next_action'],'credentials');self.assertFalse(run.called)
    def test_full_start_downloads_only_missing_channels_then_era5_without_engine(self):
        state,run=self.start()
        self.assertEqual(state['status'],'data_ready');self.assertEqual(state['next_action'],'engine')
        snapshot=run.call_args.args[0]
        self.assertTrue({'7','9','10'}.issubset(snapshot['channels']))
        self.assertEqual(sorted(Path(j['path']).name[-6:-4] for j in self.app.store.jobs()),['07','10']);self.assertGreater(self.transport.calls,0)
        self.assertTrue(run.call_args.args[2]['data_only'])
        self.assertTrue(run.call_args.args[2]['prepare_coefficients'])
        self.assertEqual(next(s['status'] for s in state['steps'] if s['key']=='channels'),'done')
        self.assertNotEqual(next(s['status'] for s in state['steps'] if s['key']=='calibration'),'done')
    def test_rerun_uses_registered_channels(self):
        self.start();calls=self.transport.calls;self.start();self.assertEqual(self.transport.calls,calls)
    def test_data_only_skips_satellite_downloads(self):
        state,run=self.start(dict(self.body,data_only=True))
        self.assertEqual(state['next_action'],'complete');self.assertFalse(self.app.store.jobs());self.assertEqual(self.transport.calls,0)
        self.assertEqual(next(s['status'] for s in state['steps'] if s['key']=='channels'),'skipped')
        self.assertFalse(run.call_args.args[2]['prepare_coefficients'])
    def test_data_only_does_not_need_scene_or_engine_or_geometry(self):
        body={'platform':'ARCM1','time':'2024-01-01T00:00:00Z','product':'night','data_only':True}
        state,run=self.start(body);self.assertEqual(state['status'],'data_ready');self.assertEqual(run.call_args.args[0]['channels'],{})
    def test_no_zenith_is_invented(self):
        state,run=self.start(runtime=dict(RUNTIME,pyrttov=True))
        self.assertEqual(state['next_action'],'geometry');self.assertIsNone(run.call_args.args[2]['zenith_deg'])
        self.assertTrue(run.call_args.args[2]['data_only'])
    def test_valid_geometry_and_engine_activate_calculation(self):
        _,run=self.start(dict(self.body,zenith_deg=23,constant_angle_acknowledged=True),runtime=dict(RUNTIME,pyrttov=True))
        self.assertFalse(run.call_args.args[2]['data_only'])
    def test_missing_confirmation_not_fabricated(self):
        _,run=self.start(dict(self.body,zenith_deg=23),runtime=dict(RUNTIME,pyrttov=True))
        self.assertTrue(run.call_args.args[2]['data_only'])
    def test_invalid_angle_rejected_before_side_effects(self):
        for z in (False,-1,71,float('nan')):
            with self.assertRaises(ValueError):self.app.era5_start(dict(self.body,zenith_deg=z))
        self.assertFalse(self.app.store.jobs())
    def test_engine_failure_does_not_claim_ready(self):
        def bad(*args):
            report=fake_report(*args);report.update(status='error',message='RTTOV: ошибка тестового расчётчика.');return report
        state,_=self.start(calculate=bad)
        self.assertEqual(state['status'],'error');self.assertNotIn('SYNTHETIC-TOKEN',json.dumps(state))
        self.assertTrue(state.get('report_id'))
    def test_unexpected_exception_hides_details(self):
        def bad(*args):raise RuntimeError('Bearer SYNTHETIC-TOKEN-NOT-SECRET')
        state,_=self.start(calculate=bad);self.assertEqual(state['status'],'error')
        self.assertNotIn('TOKEN',json.dumps(self.app.era5_state()))
    def test_partial_pass_not_all_green(self):
        def partial(*args):
            r=fake_report(*args);r.update(status='ready',channels={'7':{'status':'passed'},'9':{'status':'rejected','reason':'test'},'10':{'status':'rejected','reason':'test'}});return r
        state,_=self.start(calculate=partial)
        step=next(s for s in state['steps'] if s['key']=='calibration')
        self.assertEqual(step['status'],'waiting');self.assertIn('1/3',step['message'])
    def test_scoped_cancel_does_not_cancel_other_id(self):
        entered=threading.Event()
        def slow(*args):
            entered.set();cancel=args[-2];cancel.wait(3)
            if cancel.is_set():raise Cancelled()
            return fake_report(*args)
        with patch('arktika.era5_workflow.ensure_runtime',return_value=RUNTIME),patch('arktika.era5_workflow.run_calculation',side_effect=slow):
            started=self.app.era5_start(dict(self.body,data_only=True));self.assertTrue(entered.wait(3))
            self.assertFalse(self.app.era5_cancel({'id':'other'})['cancelled']);self.assertFalse(self.app.cancel.is_set())
            self.assertTrue(self.app.era5_cancel({'id':started['id']})['cancelled']);state=self.wait()
        self.assertEqual(state['status'],'cancelled');self.assertNotEqual(state['steps'][-1]['status'],'done')
    def test_unknown_channels_trigger_one_catalog_search(self):
        body={'platform':'ARCM1','time':'2024-01-01T00:00:00Z','product':'night','acknowledged':True}
        with patch.object(self.app.client,'stac') as search:
            state,_=self.start(body)
        self.assertEqual(search.call_count,1);self.assertEqual(state['next_action'],'archive')
        self.assertEqual(search.call_args.args[0],'ARCM1');self.assertFalse(self.app.store.jobs())
    def test_rerun_signature_and_report_never_persist_secret(self):
        self.start()
        for file in (self.root/'state'/'era5').rglob('*.json'):
            self.assertNotIn('SYNTHETIC-TOKEN-NOT-SECRET',file.read_text(encoding='utf-8'))
    def test_restart_marks_interrupted_and_preserves_progress(self):
        self.app._era5_job={'id':'test','status':'running','phase':'x','steps':[{'key':'era5','status':'running'}]};self.app._save_flow()
        del self.app._era5_credential
        self.assertEqual(self.app.era5_state()['job']['status'],'interrupted')
        self.assertEqual(self.app.era5_state()['job']['steps'][0]['status'],'waiting')
        self.assertFalse(self.app.era5_state()['credentials']['present'])
    def test_setup_success_requires_import_probe(self):
        with patch('arktika.era5_workflow.ensure_runtime',return_value=RUNTIME):
            self.app.era5_setup({});state=self.wait()
        self.assertEqual(state['status'],'setup_ready')
    def test_setup_failure_preserves_actionable_state(self):
        with patch('arktika.era5_workflow.ensure_runtime',side_effect=ValueError('Не удалось установить библиотеки.')):
            self.app.era5_setup({});state=self.wait()
        self.assertEqual(state['status'],'error');self.assertIn('установить',state['phase'])


class RuntimeTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_existing_complete_runtime_does_not_install(self):
        with patch('arktika.era5_runtime.runtime_info',return_value=RUNTIME),patch('arktika.era5_runtime.run_process') as run:
            r=ensure_runtime(self.root)
        self.assertFalse(run.called);self.assertEqual(r['missing'],[])
    def test_install_is_managed_fixed_argv_no_system_pip(self):
        exe=python_path(self.root/'era5'/'runtime');exe.parent.mkdir(parents=True)
        def process(args,*a,**kw):
            if 'venv' in args:exe.touch()
            return 0
        with patch('arktika.era5_runtime.runtime_info',return_value=dict(RUNTIME,missing=['cdsapi'])),patch('arktika.era5_runtime.run_process',side_effect=process) as run,patch('arktika.era5_runtime.probe',return_value=RUNTIME):
            result=ensure_runtime(self.root)
        self.assertEqual(run.call_count,2);args=run.call_args.args[0]
        self.assertEqual(Path(args[0]),exe);self.assertIn('--only-binary=:all:',args);self.assertNotIn('--break-system-packages',args)
        self.assertEqual(result['python'],str(exe));self.assertTrue((exe.parent.parent/'ready.json').is_file())
    def test_install_failure_is_not_readiness(self):
        with patch('arktika.era5_runtime.runtime_info',return_value=dict(RUNTIME,missing=['cdsapi'])),patch('arktika.era5_runtime.run_process',return_value=1):
            with self.assertRaises(ValueError):ensure_runtime(self.root)
        self.assertFalse((self.root/'era5'/'runtime'/'ready.json').exists())
    def test_import_failure_after_pip_is_not_success(self):
        with patch('arktika.era5_runtime.runtime_info',return_value=dict(RUNTIME,missing=['cdsapi'])),patch('arktika.era5_runtime.run_process',return_value=0),patch('arktika.era5_runtime.probe',return_value=dict(RUNTIME,missing=['netCDF4'])):
            with self.assertRaisesRegex(ValueError,'импорта'):ensure_runtime(self.root)
    def test_no_arbitrary_package_or_shell_from_input(self):
        with self.assertRaises(ValueError):configure_engine(self.root,'$(echo not-a-path)')
        self.assertFalse((self.root/'era5'/'engine.json').exists())
    def test_cancel_before_start(self):
        c=threading.Event();c.set()
        with patch('arktika.era5_runtime.subprocess.Popen') as popen:
            with self.assertRaises(Cancelled):run_process([sys.executable,'-c','pass'],c)
        self.assertFalse(popen.called)
    def test_actual_child_can_be_cancelled(self):
        c=threading.Event();t=threading.Timer(.3,c.set);t.start()
        try:
            with self.assertRaises(Cancelled):run_process([sys.executable,'-c','import time;time.sleep(20)'],c)
        finally:t.cancel()
    def test_actual_child_timeout(self):
        with self.assertRaises(ValueError):run_process([sys.executable,'-c','import time;time.sleep(20)'],timeout=.1)
    def test_probe_really_imports_engine(self):
        wrapper=self.root/'wrapper';wrapper.mkdir()
        (wrapper/'pyrttov.py').write_text('raise ImportError("SYNTHETIC engine is broken")')
        with patch.dict(os.environ,{'ARKTIKA_RTTOV_PATH':str(wrapper)}):r=probe(sys.executable,self.root,True)
        self.assertFalse(r['pyrttov'])



@unittest.skipUnless(HAS_XARRAY,'Дополнительная проверка требует xarray')
class IsolatedPipeline(unittest.TestCase):
    def test_real_child_reads_checked_cache_without_channels_or_rttov(self):
        import hashlib
        import shutil
        from arktika.download import digest
        from arktika.era5_access import plan_requests
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);files=root/'files';files.mkdir()
            plan=plan_requests('2024-01-01T00:30:00Z',[9,10],[80,0,60,40]);plan['srf_sha256']='SYNTHETIC-TEST'
            model=model_files(files,plan);cache=root/'era5'/'cache';cache.mkdir(parents=True)
            for item,source in zip(plan['requests'],model):
                key=hashlib.sha256(json.dumps({'provider':plan['provider'],**item},sort_keys=True).encode()).hexdigest()
                target=cache/(key+'.nc');shutil.copyfile(source['path'],target)
                atomic_json(cache/(key+'.json'),{'sha256':digest(target),'retrieved_at':time.time()})
            scene={'id':'ARCM2_20240101003000','time':plan['time'],'platform':'ARCM2','channels':{}}
            report=run_calculation(scene,plan,{'data_only':True},root,{},'f'*32,RUNTIME,threading.Event(),lambda _:None)
            self.assertEqual(report['status'],'data_ready');self.assertEqual(len(report['era5_files']),4)
            self.assertEqual(report['inputs'],[]);self.assertEqual(report['channels'],{})
            self.assertFalse(report['scientific_validation'])

if __name__=='__main__':unittest.main()
