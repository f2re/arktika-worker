"""Archive planning validates inputs without submitting requests or inventing results."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from arktika.download import atomic_json
from arktika.pmc_archive import build_plan
from arktika.workstation import Workstation
from arktika.pmc_detection import ALGORITHM_ID as PMC_DETECTOR_ID
from arktika.pmc_detection import ALGORITHM_VERSION as PMC_DETECTOR_VERSION


def request(**changes):
    value = {"sources": ["era5", "carra2", "merra2"],
             "start": "2024-01-01T00:00:00Z", "end": "2024-01-01T06:00:00Z",
             "area": [80, -20, 60, 70]}
    value.update(changes)
    return value


class PmcArchivePlanTests(unittest.TestCase):
    def test_plan_is_candidate_only_and_counts_each_source_native_cadence(self):
        plan = build_plan(request(), credentials={"era5": True}, cached_fields=[])
        self.assertEqual(plan["maturity"], "experimental")
        self.assertEqual(plan["output_status"], "candidate_only")
        self.assertFalse(plan["forecast"])
        sources = {row["id"]: row for row in plan["sources"]}
        self.assertEqual(sources["era5"]["step_count"], 7)
        self.assertEqual(sources["carra2"]["step_count"], 3)
        self.assertEqual(sources["merra2"]["step_count"], 3)
        self.assertEqual(sources["era5"]["credential_status"], "ready")
        self.assertEqual(sources["merra2"]["credential_status"], "required")
        self.assertEqual(plan["satellite"]["status"], "manual_mask_required")
        self.assertIn("не запускает расчёт", plan["missing_inputs"][0])

    def test_matching_cached_wind_fields_are_reported_but_not_treated_as_derived_results(self):
        fields = [{"source": "era5", "variable": "wind", "level": 850,
                   "time": "2024-01-01T00:00:00Z"}]
        plan = build_plan(request(sources=["era5"]), credentials={}, cached_fields=fields)
        self.assertEqual(plan["sources"][0]["cached_step_count"], 1)
        self.assertEqual(plan["sources"][0]["missing_step_count"], 6)
        self.assertEqual(plan["output_status"], "candidate_only")

    def test_rejects_naive_times_bad_sources_future_and_invalid_area(self):
        for changes in (
            {"start": "2024-01-01T00:00:00"},
            {"sources": ["unknown"]},
            {"area": [39, -20, 30, 70]},
            {"start": "2024-01-01T06:00:00Z", "end": "2024-01-01T00:00:00Z"},
            {"sources": ["carra2"], "start": "1980-01-01T00:00:00Z"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_plan(request(**changes), credentials={}, cached_fields=[])

    def test_workstation_api_returns_only_plan_without_creating_a_job_or_product(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Workstation(root / "state", download_dir=root / "downloads")
            try:
                result = app.pmc_archive_plan(request(sources=["era5"]))
                self.assertEqual(result["output_status"], "candidate_only")
                self.assertFalse(app.busy)
                self.assertEqual(list(app.fields_state()["fields"]), [])
                self.assertEqual(app.products(), [])
            finally:
                app.close()

    def test_cached_carra2_fields_can_run_candidate_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);app=Workstation(root/'state',download_dir=root/'downloads')
            identity='c'*24
            field={'id':identity,'source':'carra2','variable':'wind','level':850,
                   'time_kind':'instantaneous_analysis','time':'2024-01-01T00:00:00Z'}
            grid={'crs':'EPSG:3413','width':20,'height':20,
                  'transform':[10000,0,-100000,0,-10000,100000],
                  'resolution_x_m':10000,'resolution_y_m':10000}
            plan=request(sources=['carra2'],start='2024-01-01T00:00:00Z',end='2024-01-01T00:00:00Z')
            result={'algorithm_id':PMC_DETECTOR_ID,'algorithm_version':PMC_DETECTOR_VERSION,
                    'maturity':'experimental','source':'carra2','valid_time':field['time'],
                    'grid':dict(grid),'candidate_count':0,'candidates':[]}
            try:
                with patch.object(app,'fields_state',return_value={'fields':[field]}), \
                     patch.object(app,'field',return_value=field), \
                     patch.object(app,'experimental_pmc_candidates',return_value=result):
                    started=app.pmc_archive_start({'plan':plan,'field_ids':[identity],
                                                   'target_grid':grid})
                    self.assertEqual('running',started['status'])
                    deadline=time.monotonic()+5
                    while time.monotonic()<deadline:
                        state=app.pmc_archive_state()
                        if state['status']!='running':break
                        time.sleep(.01)
                    self.assertEqual('completed',state['status'])
                    self.assertTrue(app.pmc_archive_report(started['id']))
            finally:
                app.close()

    def test_cached_archive_run_persists_partial_report_and_separates_time_segments(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);app=Workstation(root/'state',download_dir=root/'downloads')
            ids=['a'*24,'b'*24]
            fields=[{'id':ids[0],'source':'era5','variable':'wind','level':850,
                     'time_kind':'instantaneous_analysis','time':'2024-01-01T00:00:00Z'},
                    {'id':ids[1],'source':'era5','variable':'wind','level':850,
                     'time_kind':'instantaneous_analysis','time':'2024-01-01T02:00:00Z'}]
            grid={'crs':'EPSG:3413','width':20,'height':20,
                  'transform':[10000,0,-100000,0,-10000,100000],
                  'resolution_x_m':10000,'resolution_y_m':10000}
            plan=request(sources=['era5'],end='2024-01-01T02:00:00Z',access_token='must-not-persist',
                         private_uri='https://example.invalid/private?signature=secret')
            def result_for(identity,**kwargs):
                meta=next(field for field in fields if field['id']==identity)
                return {'algorithm_id':PMC_DETECTOR_ID,'algorithm_version':PMC_DETECTOR_VERSION,
                        'maturity':'experimental','source':meta['source'],'valid_time':meta['time'],
                        'grid':dict(grid),'candidate_count':1,
                        'candidates':[{'candidate_id':'candidate-'+meta['time'][11:13],
                            'centroid_pixel_rc':[10.0,10.0],'classification':'candidate',
                            'confirmation_status':'not_confirmed'}]}
            try:
                with patch.object(app,'fields_state',return_value={'fields':fields}), \
                     patch.object(app,'field',side_effect=lambda identity:next(f for f in fields if f['id']==identity)), \
                     patch.object(app,'experimental_pmc_candidates',side_effect=result_for):
                    started=app.pmc_archive_start({'plan':plan,'field_ids':ids,'target_grid':grid})
                    persisted=(root/'state'/'reanalysis'/'pmc_archive_job.json').read_text(encoding='utf-8')
                    self.assertNotIn('must-not-persist',persisted)
                    self.assertNotIn('signature=secret',persisted)
                    self.assertEqual({'sources','start','end','area','step_count'},set(json.loads(persisted)['plan']))
                    deadline=time.monotonic()+5
                    while time.monotonic()<deadline:
                        state=app.pmc_archive_state()
                        if state['status']!='running':break
                        time.sleep(.01)
                    self.assertEqual('partial',state['status'],state)
                    report=__import__('json').loads(app.pmc_archive_report(started['id']))
                    self.assertEqual('candidate_only',report['output_status'])
                    self.assertEqual(3,report['coverage'][0]['expected_steps'])
                    self.assertEqual(2,report['coverage'][0]['processed_steps'])
                    self.assertEqual(1,report['coverage'][0]['unprocessed_steps'])
                    self.assertEqual(2,report['tracks_by_source'][0]['track_count'])
                    self.assertTrue(all(track['left_censored'] for track in report['tracks_by_source'][0]['tracks']))
            finally:
                app.close()

            reopened=Workstation(root/'state',download_dir=root/'downloads')
            try:
                with patch.object(reopened,'fields_state',return_value={'fields':fields}):
                    state=reopened.pmc_archive_state()
                self.assertEqual('partial',state['status'])
                self.assertEqual(2,state['completed'])
            finally:
                reopened.close()

            job_path=root/'state'/'reanalysis'/'pmc_archive_job.json'
            persisted=json.loads(job_path.read_text(encoding='utf-8'))
            persisted.update(status='running',resume_available=False)
            atomic_json(job_path,persisted)
            resumed=Workstation(root/'state',download_dir=root/'downloads')
            try:
                self.assertEqual('interrupted',resumed.pmc_archive_state()['status'])
                with patch.object(resumed,'field',side_effect=lambda identity:next(f for f in fields if f['id']==identity)), \
                     patch.object(resumed,'fields_state',return_value={'fields':fields}):
                    response=resumed.pmc_archive_resume({'id':started['id']})
                    self.assertTrue(response['started'])
                    deadline=time.monotonic()+5
                    while time.monotonic()<deadline:
                        state=resumed.pmc_archive_state()
                        if state['status']!='running':break
                        time.sleep(.01)
                    self.assertEqual('partial',state['status'])
                    self.assertEqual(2,state['completed'])
            finally:
                resumed.close()

    def test_remote_preparation_persists_safe_specs_and_stops_before_detector(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);app=Workstation(root/'state',download_dir=root/'downloads')
            grid={'crs':'EPSG:3413','width':20,'height':20,
                  'transform':[10000,0,-100000,0,-10000,100000],
                  'resolution_x_m':10000,'resolution_y_m':10000}
            plan=request(sources=['era5'],end='2024-01-01T02:00:00Z',
                         access_token='do-not-save',private_uri='https://private.invalid/?sig=secret')
            metas=[]
            def field_process(payload,**kwargs):
                meta={'id':payload['identity'],'source':'era5','variable':'wind','level':850,
                      'time_kind':'instantaneous_analysis','time':payload['plan']['time']}
                metas.append(meta)
                return {'field':meta,'cached':False}
            def load_field(identity):
                for meta in metas:
                    if meta['id']==identity:return meta
                raise ValueError('Field is not cached yet.')
            try:
                with patch.object(app,'_adapter_credential',return_value={'token':'do-not-save'}), \
                     patch.object(app,'_field_process',side_effect=field_process), \
                     patch.object(app,'field',side_effect=load_field), \
                     patch.object(app,'experimental_pmc_candidates') as detector:
                    started=app.pmc_archive_prepare({'plan':plan,'target_grid':grid})
                    self.assertFalse(started['detector_started'])
                    deadline=time.monotonic()+5
                    while time.monotonic()<deadline:
                        state=app.pmc_archive_prepare_state()
                        if state['status']!='running':break
                        time.sleep(.01)
                    self.assertEqual('ready',state['status'])
                    self.assertEqual(3,state['prepared'])
                    detector.assert_not_called()
                    saved=(root/'state'/'reanalysis'/'pmc_prepare_job.json').read_text(encoding='utf-8')
                    self.assertNotIn('do-not-save',saved)
                    self.assertNotIn('private.invalid',saved)
                    self.assertNotIn('sig=secret',saved)
                    parsed=json.loads(saved)
                    self.assertEqual({'source','variable','time','level','area'},set(parsed['specs'][0]))
                    self.assertEqual(3,len(parsed['completed_field_ids']))
            finally:
                app.close()

    def test_missing_credentials_pause_and_resume_with_current_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);app=Workstation(root/'state',download_dir=root/'downloads')
            grid={'crs':'EPSG:3413','width':20,'height':20,
                  'transform':[10000,0,-100000,0,-10000,100000],
                  'resolution_x_m':10000,'resolution_y_m':10000}
            plan=request(sources=['era5'],start='2024-01-01T00:00:00Z',end='2024-01-01T00:00:00Z')
            meta={'id':'a'*24,'source':'era5','variable':'wind','level':850,
                  'time_kind':'instantaneous_analysis','time':'2024-01-01T00:00:00Z'}
            completed=[]
            def load_field(identity):
                if completed:return meta
                raise ValueError('Field is not cached yet.')
            try:
                with patch.object(app,'_adapter_credential',return_value={}), \
                     patch.object(app,'_field_process') as worker:
                    started=app.pmc_archive_prepare({'plan':plan,'target_grid':grid})
                    deadline=time.monotonic()+5
                    while time.monotonic()<deadline:
                        state=app.pmc_archive_prepare_state()
                        if state['status']!='running':break
                        time.sleep(.01)
                    self.assertEqual('needs_action',state['status'])
                    self.assertEqual('credentials_required',state['needs_action']['code'])
                    worker.assert_not_called()
                    saved=(root/'state'/'reanalysis'/'pmc_prepare_job.json').read_text(encoding='utf-8')
                    self.assertNotIn('"credential":',saved)
                    self.assertEqual('preparing',app.pmc_archive_cancel({'id':started['id']})['stage'])
                    state=app.pmc_archive_prepare_state()
                    self.assertEqual('cancelled',state['status'])
                    self.assertTrue(state['resume_available'])
                    with patch.object(app,'_adapter_credential',return_value={'token':'new-secret'}), \
                     patch.object(app,'_field_process',side_effect=lambda *args,**kwargs:(completed.append(True) or {'field':meta,'cached':False})) as resume_worker, \
                         patch.object(app,'field',side_effect=load_field):
                        app.pmc_archive_prepare_resume({'id':started['id']})
                        deadline=time.monotonic()+5
                        while time.monotonic()<deadline:
                            state=app.pmc_archive_prepare_state()
                            if state['status']!='running':break
                            time.sleep(.01)
                        self.assertEqual('ready',state['status'])
                        self.assertEqual('new-secret',resume_worker.call_args.args[0]['credential']['token'])
                        persisted=(root/'state'/'reanalysis'/'pmc_prepare_job.json').read_text(encoding='utf-8')
                        self.assertNotIn('new-secret',persisted)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
