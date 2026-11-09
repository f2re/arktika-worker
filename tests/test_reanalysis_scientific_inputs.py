"""Provider-contract and context-workflow checks; fixtures are not observations."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from arktika.pmc_archive import area_contains, build_plan
from arktika.reanalysis.catalog import LEVELS, pmc_input_specs, request_plan, supports
from arktika.workstation import Workstation
from test_reanalysis import HAS_FIELDS, dataset, plan


STAMP = '2024-01-01T00:00:00Z'
AREA = [75., 20., 65., 40.]
GRID = {'crs': 'EPSG:3413', 'width': 20, 'height': 20,
        'transform': [10000, 0, -100000, 0, -10000, 100000],
        'resolution_x_m': 10000, 'resolution_y_m': 10000}


def archive_request(sources=None):
    return {'sources': sources or ['era5'], 'start': STAMP, 'end': STAMP, 'area': list(AREA)}


class ProviderCatalogues(unittest.TestCase):
    def test_carra_pressure_levels_follow_pressure_table_not_union_enum(self):
        self.assertEqual(len(LEVELS['carra2']), 20)
        self.assertIn(70, LEVELS['carra2'])
        for unsupported in (30, 10):
            spec = dict(plan('carra2'), level=unsupported)
            with self.subTest(level=unsupported), self.assertRaises(ValueError):
                request_plan(spec)
        self.assertFalse(supports('carra2', 'q'))
        with self.assertRaises(ValueError):
            plan('carra2', 'q')

    def test_context_requests_are_same_source_time_and_domain(self):
        for source in ('era5', 'carra2', 'merra2'):
            specs = pmc_input_specs(source, STAMP, AREA, include_context=True)
            self.assertEqual(specs[0]['variable'], 'wind')
            self.assertEqual(specs[0]['level'], 850)
            self.assertEqual(len(specs), 4 if source == 'merra2' else 7)
            for spec in specs:
                result = request_plan(spec)
                self.assertEqual(result['source'], source)
                self.assertEqual(result['time'], STAMP)
                self.assertEqual(result['area'], AREA)
            t500 = next(spec for spec in specs if spec['variable'] == 't')
            self.assertEqual(t500['level'], 500)
            if source != 'merra2':
                lsm = request_plan(next(spec for spec in specs if spec['variable'] == 'lsm'))
                self.assertIn('land_sea_mask', lsm['requests'][0]['request']['variable'])

    def test_missing_context_is_explicit_and_never_substituted(self):
        result = build_plan(archive_request(['merra2']), credentials={}, cached_fields=[])
        missing = {row['variable'] for row in result['sources'][0]['context_fields'] if not row['available']}
        self.assertEqual(missing, {'sst', 'ice', 'lsm'})
        self.assertEqual(result['output_status'], 'candidate_only')

    def test_cache_domain_handles_dateline_and_rejects_small_or_unknown_area(self):
        self.assertTrue(area_contains([80, 160, 60, -160], [75, 170, 65, -170]))
        self.assertTrue(area_contains([90, -180, 0, 180], [75, 170, 65, -170]))
        self.assertFalse(area_contains([80, -170, 60, 170], [75, 170, 65, -170]))
        self.assertFalse(area_contains(None, AREA))
        self.assertFalse(area_contains([74, 20, 65, 40], AREA))

    def test_wrong_domain_and_averaged_cache_do_not_count(self):
        field = {'id': 'a'*24, 'source': 'era5', 'variable': 'wind', 'level': 850,
                 'time': STAMP, 'time_kind': 'instantaneous_analysis', 'area': list(AREA)}
        for change in ({'area': [74, 20, 65, 40]}, {'time_kind': 'time_mean'},
                       {'difference_of': ['a', 'b']}):
            with self.subTest(change=change):
                result = build_plan(archive_request(), credentials={}, cached_fields=[dict(field, **change)])
                self.assertEqual(result['sources'][0]['cached_step_count'], 0)

    def test_detector_domain_is_checked_before_downloading(self):
        for area in ([90, 20, 60, 40], [87, 20, 60, 40], [60, 20, -10, 40]):
            with self.subTest(area=area), self.assertRaisesRegex(ValueError, 'Северном полушарии'):
                build_plan(dict(archive_request(), area=area), credentials={}, cached_fields=[])


@unittest.skipUnless(HAS_FIELDS, 'Optional field dependencies unavailable')
class ProviderNormalization(unittest.TestCase):
    def test_merra_documented_long_names_define_geographic_vectors(self):
        from arktika.reanalysis.normalize import normalize
        for kind, names in [('wind', ('U', 'V')), ('wind10', ('U10M', 'V10M'))]:
            ds = dataset('merra2', kind)
            for name, direction in zip(names, ('eastward_wind', 'northward_wind')):
                ds[name].attrs.pop('standard_name')
                ds[name].attrs['long_name'] = ('10-meter_' if kind == 'wind10' else '') + direction
            result = normalize(ds, plan('merra2', kind))
            np.testing.assert_allclose(result.u, 10)
            np.testing.assert_allclose(result.v, 0)
            ds[names[0]].attrs['standard_name'] = 'grid_eastward_wind'
            with self.assertRaises(ValueError):
                normalize(ds, plan('merra2', kind))

    def test_merra_long_name_exception_is_not_applied_to_era5(self):
        from arktika.reanalysis.normalize import normalize
        ds = dataset(variable='wind')
        for name, direction in [('u', 'eastward_wind'), ('v', 'northward_wind')]:
            ds[name].attrs.pop('standard_name')
            ds[name].attrs['long_name'] = direction
        with self.assertRaises(ValueError):
            normalize(ds, plan(variable='wind'))

    def test_carra_degree_true_units_are_accepted_with_correct_from_convention(self):
        from arktika.reanalysis.normalize import normalize
        ds = dataset('carra2', 'wind')
        ds.wind_direction.attrs = {'units': 'Degree true'}
        ds.wind_direction.values[:] = 270
        result = normalize(ds, plan('carra2', 'wind'))
        np.testing.assert_allclose(result.u, 10, atol=1e-5)
        np.testing.assert_allclose(result.v, 0, atol=1e-5)
        ds.wind_direction.attrs['GRIB_uvRelativeToGrid'] = 1
        with self.assertRaises(ValueError):
            normalize(ds, plan('carra2', 'wind'))

    def test_land_mask_preserves_missing_values_and_fraction_semantics(self):
        from arktika.reanalysis.normalize import normalize
        ds = dataset(variable='ice').rename({'ci': 'lsm'})
        ds.lsm.attrs['units'] = 'Dimensionless'
        ds.lsm.values[:] = .25
        ds.lsm.values[0, 0, 0] = np.nan
        result = normalize(ds, plan(variable='lsm'))
        self.assertEqual(result.value.attrs['standard_name'], 'land_area_fraction')
        self.assertAlmostEqual(float(result.value[1, 1]), 25)
        self.assertTrue(np.isnan(result.value[0, 0]))
        ds.lsm.values[0, 1, 1] = 1.2
        with self.assertRaises(ValueError):
            normalize(ds, plan(variable='lsm'))


class ContextPreparation(unittest.TestCase):
    def test_prepare_separates_context_from_detection_and_resumes_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Workstation(Path(temp)/'state')
            cached = {}
            calls = []
            def field(identity):
                if identity not in cached:
                    raise ValueError('not cached')
                return cached[identity]
            def worker(payload, **kwargs):
                spec = payload['plan']
                row = {key: spec[key] for key in ('source', 'variable', 'level', 'time', 'area')}
                row.update(id=payload['identity'], time_kind='instantaneous_analysis')
                cached[row['id']] = row
                calls.append(spec)
                return {'field': row}
            try:
                with patch.object(app, '_adapter_credential', return_value={'token': 'TEST-ONLY'}), \
                     patch.object(app, 'field', side_effect=field), \
                     patch.object(app, '_field_process', side_effect=worker), \
                     patch.object(app, 'experimental_pmc_candidates') as detector:
                    start = app.pmc_archive_prepare({'plan': archive_request(), 'target_grid': GRID, 'include_context': True})
                    app.task.join(10)
                    state = app.pmc_archive_prepare_state()
                    self.assertEqual(state['status'], 'ready', state)
                    self.assertEqual(state['prepared'], 7)
                    self.assertEqual(len(state['field_ids']), 1)
                    self.assertEqual(len(state['context_field_ids']), 6)
                    self.assertEqual(state['context_status'], 'expert_review_required')
                    self.assertFalse(start['detector_started'])
                    detector.assert_not_called()
                    persisted = (app.fields_root/'pmc_prepare_job.json').read_text(encoding='utf-8')
                    self.assertNotIn('TEST-ONLY', persisted)
                    self.assertEqual(json.loads(persisted)['plan']['step_count'], 1)
                    app._pmc_prepare_job['status'] = 'interrupted'
                    app._pmc_prepare_job['resume_available'] = True
                    app._persist_pmc_prepare_job()
            finally:
                app.close()
            resumed = Workstation(Path(temp)/'state')
            try:
                with patch.object(resumed, 'field', side_effect=field), \
                     patch.object(resumed, '_field_process') as worker_again:
                    resumed.pmc_archive_prepare_resume({'id': start['id']})
                    resumed.task.join(10)
                    self.assertEqual(resumed.pmc_archive_prepare_state()['status'], 'ready')
                    worker_again.assert_not_called()
            finally:
                resumed.close()

    def test_plan_excludes_cache_with_corrupt_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Workstation(Path(temp)/'state')
            app._fields_init()
            identity = 'a'*24
            folder = app.field_cache/identity
            folder.mkdir()
            (folder/'field.nc').write_bytes(b'changed bytes')
            metadata = {'id': identity, 'source': 'era5', 'variable': 'wind', 'level': 850,
                        'time': STAMP, 'area': AREA, 'time_kind': 'instantaneous_analysis',
                        'sha256': '0'*64}
            (folder/'metadata.json').write_text(json.dumps(metadata))
            try:
                result = app.pmc_archive_plan(archive_request())
                self.assertEqual(result['sources'][0]['cached_step_count'], 0)
            finally:
                app.close()

    def test_non_boolean_include_context_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Workstation(Path(temp)/'state')
            try:
                with self.assertRaises(ValueError):
                    app.pmc_archive_prepare({'plan': archive_request(), 'target_grid': GRID, 'include_context': 'false'})
            finally:
                app.close()


if __name__ == '__main__':
    unittest.main()
