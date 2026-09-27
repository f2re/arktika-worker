"""Regressions for table preparation without RTTOV. Fixtures are not observations."""
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

from arktika.download import atomic_json
from arktika.era5_calculation import calculate_reference
from arktika.era5_forward import COEF_NAME, coefficient_info, download_coefficients
from arktika.network import Cancelled
from test_era5_workflow import workflow_fixture, RUNTIME, fake_report

TABLE=b'! SYNTHETIC SOFTWARE COEFFICIENT FIXTURE, NOT REAL RTTOV\n'+b'1 2 3\n'*300

def archive_bytes(name=COEF_NAME,table=TABLE):
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:bz2') as archive:
        member=tarfile.TarInfo('rttov13pred54L/'+name);member.size=len(table)
        archive.addfile(member,io.BytesIO(table))
    return stream.getvalue()

class Downloader(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{},clear=False);self.env.start()
        os.environ.pop('ARKTIKA_ELECTROL_COEF',None)
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def download(self,data=None,cancel=None,progress=lambda m:None):
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=io.BytesIO(archive_bytes() if data is None else data)
            return download_coefficients(self.root,cancel,progress)
    def test_download_validated_and_cached_without_engine(self):
        info=self.download();self.assertTrue(info['present']);self.assertEqual(info['size_bytes'],len(TABLE))
        self.assertEqual(info['origin'],'official_https_download')
        with patch('urllib.request.build_opener',side_effect=AssertionError('Must reuse table')):
            self.assertEqual(download_coefficients(self.root)['sha256'],info['sha256'])
    def test_cancel_before_network(self):
        cancel=threading.Event();cancel.set()
        with patch('urllib.request.build_opener',side_effect=AssertionError('No network')):
            with self.assertRaises(Cancelled):download_coefficients(self.root,cancel)
    def test_progress_has_table_specific_prefix(self):
        messages=[];self.download(progress=messages.append)
        self.assertGreaterEqual(len(messages),2)
        self.assertTrue(all(m.startswith('Коэффициенты:') for m in messages))
    def test_no_cds_or_era5_environment_required(self):
        with patch.dict(os.environ,{},clear=True):self.assertTrue(self.download()['present'])
    def test_wrong_table_rejected(self):
        with self.assertRaisesRegex(ValueError,'нет ожидаемого'):self.download(archive_bytes(name='other.dat'))
        self.assertFalse(coefficient_info(self.root)['present'])
    def test_html_not_a_table(self):
        with self.assertRaises(ValueError):self.download(b'<html>server error</html>')
        self.assertFalse(coefficient_info(self.root)['present'])
    def test_table_with_html_rejected(self):
        with self.assertRaises(ValueError):self.download(archive_bytes(table=b'<html>'+b'x'*2000))
        self.assertFalse(coefficient_info(self.root)['present'])
    def test_interrupted_file_not_marked_ready(self):
        with self.assertRaises(ValueError):self.download(archive_bytes()[:30])
        self.assertFalse(coefficient_info(self.root)['present'])
        self.assertFalse(list(self.root.rglob('*.part')))
    def test_network_error_is_actionable_and_redacted(self):
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=urllib.error.URLError('SENSITIVE-NETWORK-DETAILS')
            with self.assertRaises(ValueError) as caught:download_coefficients(self.root)
        self.assertNotIn('SENSITIVE',str(caught.exception));self.assertIn('Повторите',str(caught.exception))

class Calculation(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.scene={'id':'test','platform':'ARCM2','time':'2024-01-01T00:00:00Z','channels':{}}
        self.plan={'channels':[9]}
    def tearDown(self):self.tmp.cleanup()
    def run_case(self,options,side_effect=None):
        info={'present':True,'name':COEF_NAME,'sha256':'0'*64,'size_bytes':len(TABLE)}
        with patch('arktika.era5_calculation.retrieve_plan',return_value=[{'group':'test','path':'private'}]),patch('arktika.era5_calculation.load_group'),patch('arktika.era5_calculation.download_coefficients',return_value=info,side_effect=side_effect) as get,patch('arktika.era5_calculation.forward_isolated',side_effect=AssertionError('No engine')):
            result=calculate_reference(self.scene,self.plan,options,self.root,{},'1'*32)
        return result,get
    def test_prepare_without_engine_fetches_coefficients(self):
        report,get=self.run_case({'data_only':True,'prepare_coefficients':True})
        self.assertTrue(get.called);self.assertEqual(report['status'],'data_ready')
        self.assertTrue(report['coefficient']['present']);self.assertEqual(report['channels'],{})
    def test_explicit_data_only_still_skips_table(self):
        report,get=self.run_case({'data_only':True})
        self.assertFalse(get.called);self.assertEqual(report['status'],'data_ready')
    def test_failed_table_keeps_model_files_and_reason(self):
        report,get=self.run_case({'data_only':True,'prepare_coefficients':True},ValueError('NWP SAF недоступен.'))
        self.assertEqual(report['status'],'error');self.assertEqual(report['next_action'],'coefficients')
        self.assertEqual(len(report['era5_files']),1);self.assertNotIn('coefficient',report)
    def test_cancelled_table_does_not_mark_ready(self):
        with self.assertRaises(Cancelled):self.run_case({'data_only':True,'prepare_coefficients':True},Cancelled())
        report=json.loads((self.root/'era5/runs'/('1'*32)/'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'],'cancelled')

class Resume(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.app,self.scene,_,_=workflow_fixture(self.root)
        self.app._era5_init()
        self.app._era5_job={'id':'2'*32,'status':'data_ready','scene':self.scene['id'],'product':'micro24','data_only':False,
            'phase':'Old pause','next_action':'engine','report_id':'2'*32,'steps':[{'key':'era5','status':'done'},{'key':'engine','status':'waiting'}]}
        self.app._save_flow()
    def tearDown(self):self.app.cancel.set();self.wait();self.app.close();self.tmp.cleanup()
    def wait(self):
        until=time.monotonic()+5
        while self.app.busy and time.monotonic()<until:time.sleep(.02)
        self.assertFalse(self.app.busy);return self.app._era5_job
    def start(self,error=None):
        with patch('arktika.auto_calibration.coefficient_info',return_value={'present':False}),patch('arktika.auto_calibration.download_coefficients',return_value={'present':True},side_effect=error) as get,patch('arktika.era5_workflow.run_calculation',side_effect=AssertionError('No ERA5 retrieval')):
            self.app.era5_coefficient_download({'acknowledged':True});job=self.wait()
        self.assertTrue(get.called);return job
    def test_old_pause_recovers_without_credentials_or_model_request(self):
        job=self.start();self.assertEqual(job['status'],'data_ready');self.assertEqual(job['next_action'],'engine')
        self.assertEqual(job['report_id'],'2'*32);self.assertFalse(self.app._era5_credential)
        self.assertEqual(next(s['status'] for s in job['steps'] if s['key']=='coefficients'),'done')
    def test_retry_preserves_old_report_and_pause(self):
        first=self.start(ValueError('network'));self.assertEqual(first['status'],'error');self.assertEqual(first['next_action'],'coefficients')
        job=self.start();self.assertEqual(job['status'],'data_ready');self.assertEqual(job['report_id'],'2'*32)
    def test_cancel_table_does_not_erase_era5(self):
        job=self.start(Cancelled());self.assertEqual(job['status'],'cancelled')
        self.assertEqual(job['report_id'],'2'*32);self.assertEqual(job['steps'][0]['status'],'done')
    def test_cached_table_is_not_scheduled_again(self):
        with patch('arktika.auto_calibration.coefficient_info',return_value={'present':True}),patch('arktika.auto_calibration.download_coefficients') as get:
            result=self.app.era5_coefficient_download({'acknowledged':True})
        self.assertFalse(result['started']);self.assertFalse(get.called)
    def test_missing_acknowledgement_rejected(self):
        with self.assertRaises(ValueError):self.app.era5_coefficient_download({})
    def test_unknown_exception_does_not_expose_details(self):
        job=self.start(RuntimeError('SENSITIVE-DETAILS'));self.assertNotIn('SENSITIVE',json.dumps(job))

if __name__=='__main__':unittest.main()
