"""Analytic contract fixtures, never evidence of real weather or live access."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np

from arktika.analysis_service import AnalysisMixin
from arktika.reanalysis.normalize import normalize
from arktika.source_validation import reject_explicit_synthetic_metadata
from test_interpretation import profile
from test_reanalysis import HAS_FIELDS, dataset, plan


STAMP = '2024-01-01T00:00:00Z'


class ExplicitOriginTests(unittest.TestCase):
    def test_declared_artificial_origin_is_refused(self):
        for metadata in ({'data_kind': 'synthetic'}, {'data_kind': 'synthetic_demo'},
                         {'profile_kind': 'demo'}, {'title': 'SYNTHETIC NUMERICAL VORTEX'},
                         {'title': 'SYNTHETIC PRESSURE FOR SOFTWARE TEST'},
                         {'source': 'SYNTHETIC SOFTWARE TEST; NOT OBSERVATIONS'},
                         {'source': 'Демонстрационный синтетический профиль (ICAO/COESA)'}):
            with self.subTest(metadata=metadata), self.assertRaisesRegex(ValueError, 'Синтетические'):
                reject_explicit_synthetic_metadata(metadata)

    def test_processing_history_or_filename_does_not_infer_data_origin(self):
        reject_explicit_synthetic_metadata({'source': 'ERA5', 'title': 'Pressure analysis',
            'history': 'Software test compared synthetic cases before release',
            'filename': 'synthetic-name.nc'})

    def test_profile_input_and_saved_profile_do_not_erase_synthetic_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = AnalysisMixin()
            app.store = SimpleNamespace(root=Path(tmp))
            artificial = dict(profile(), data_kind='synthetic_demo')
            for payload in (artificial, {'text': json.dumps(artificial)},
                            {'text': json.dumps(profile()), 'metadata': {'data_kind': 'synthetic'}}):
                with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, 'Синтетические'):
                    app.import_profile(payload)
            identity = 'a'*24
            (app._analysis_root('profiles')/(identity+'.json')).write_text(
                json.dumps(dict(artificial, id=identity)), encoding='utf-8')
            self.assertEqual(app.profiles(), [])
            with self.assertRaisesRegex(ValueError, 'Синтетические'):
                app.get_profile(identity)
            self.assertIsNone(app.resolve_auto_profile(70, 30, artificial['valid_time']))
            (app._analysis_root('analyses')/(identity+'.json')).write_text(
                json.dumps({'profile_kind': 'synthetic_demo', 'profile_snapshot': artificial}),
                encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Синтетические'):
                app.analysis_export(identity)


@unittest.skipUnless(HAS_FIELDS, 'Optional field dependencies unavailable')
class ReanalysisOriginTests(unittest.TestCase):
    def test_normalization_refuses_origin_before_relabelling_source(self):
        for metadata in ({'data_kind': 'synthetic'}, {'data_kind': 'synthetic_demo'},
                         {'title': 'SYNTHETIC NUMERICAL VORTEX'},
                         {'source': 'Синтетический ветер'}):
            ds = dataset(variable='wind')
            ds.attrs.update(metadata)
            with self.subTest(metadata=metadata), self.assertRaisesRegex(ValueError, 'Синтетические'):
                normalize(ds, plan(variable='wind'))
        ds = dataset(variable='wind')
        ds.u.attrs['data_kind'] = 'synthetic'
        with self.assertRaisesRegex(ValueError, 'Синтетические'):
            normalize(ds, plan(variable='wind'))

    def test_local_import_refusal_does_not_publish_a_field(self):
        from arktika.reanalysis.worker import prepare
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ds = dataset(variable='wind')
            ds.attrs['title'] = 'SYNTHETIC NUMERICAL VORTEX'
            source = root/'input.nc'
            ds.to_netcdf(source, engine='scipy')
            identity = 'b'*24
            with self.assertRaisesRegex(ValueError, 'Синтетические'):
                prepare({'cache': str(root/'cache'), 'identity': identity,
                         'path': str(source), 'plan': plan(variable='wind')}, lambda _: None)
            self.assertFalse((root/'cache'/identity).exists())


@unittest.skipUnless(HAS_FIELDS, 'Optional field dependencies unavailable')
class CachedProfileTimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = AnalysisMixin()
        self.app.store = SimpleNamespace(root=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def field(self, times=('2024-01-01T00:00:00',)):
        import xarray as xr
        shape = (len(times), 3, 1, 1)
        return xr.Dataset({
            'z': (('time', 'pressure_level', 'latitude', 'longitude'),
                  np.broadcast_to(np.array([0., 1000., 2000.])[None, :, None, None]*9.80665, shape),
                  {'units': 'm2 s-2'}),
            't': (('time', 'pressure_level', 'latitude', 'longitude'),
                  np.broadcast_to(np.array([280., 274., 268.])[None, :, None, None], shape),
                  {'units': 'K'})}, coords={
                    'time': np.asarray(times, dtype='datetime64[ns]'),
                    'pressure_level': ('pressure_level', [1000., 900., 800.], {'units': 'hPa'}),
                    'latitude': [70.], 'longitude': [30.]})

    def extract(self, ds, wanted=STAMP):
        path = self.root/'profile.nc'
        ds.to_netcdf(path, engine='scipy')
        return self.app._extract_era5_profile(path, 70., 30., wanted)

    def test_cf_time_is_exact_and_is_preserved_with_era5_origin(self):
        result = self.extract(self.field(), '2024-01-01T03:00:00+03:00')
        self.assertEqual(result['valid_time'], STAMP)
        self.assertEqual(result['data_kind'], 'era5')
        self.assertEqual(result['temperature'], [280., 274., 268.])
        self.assertEqual(self.extract(self.field().rename({'time': 'valid_time'}))['valid_time'], STAMP)

    def test_missing_wrong_duplicate_and_undecoded_times_are_unavailable(self):
        cases = (self.field(('2024-01-01T03:00:00',)), self.field().drop_vars('time'),
                 self.field(('2024-01-01T00:00:00', '2024-01-01T00:00:00')),
                 self.field().assign_coords(time=[0]))
        for ds in cases:
            with self.subTest(coords=list(ds.coords)):
                self.assertIsNone(self.extract(ds))
        self.assertIsNone(self.extract(self.field(), None))

    def test_exact_time_is_selected_from_multiple_steps(self):
        ds = self.field(('2024-01-01T00:00:00', '2024-01-01T03:00:00')).copy(deep=True)
        ds.t.values[1] -= 5
        result = self.extract(ds, '2024-01-01T03:00:00Z')
        self.assertEqual(result['valid_time'], '2024-01-01T03:00:00Z')
        self.assertEqual(result['temperature'], [275., 269., 263.])

    def test_geopotential_is_converted_to_approximate_geometric_height(self):
        ds = self.field().copy(deep=True)
        geometric = np.array([0., 10000., 20000.])
        radius, gravity = 6371229., 9.80665
        # Independently specified spherical potential difference at known
        # geometric altitudes; division by g0 alone would be ~63 m low at 20 km.
        potential = gravity*radius**2*(1/radius - 1/(radius+geometric))
        ds.z.values[0, :, 0, 0] = potential
        result = self.extract(ds)
        np.testing.assert_allclose(result['height'], geometric, rtol=0, atol=1e-6)
        self.assertIn('сферическое приближение', result['source'])
        self.assertIn('горизонтальные изменения', result['note'])

    def test_declared_synthetic_cache_profile_is_unavailable(self):
        ds = self.field()
        ds.attrs['data_kind'] = 'synthetic'
        self.assertIsNone(self.extract(ds))

    def test_auto_selection_checks_actual_profile_applicability(self):
        cache = self.root/'era5'/'cache'
        cache.mkdir(parents=True)
        self.field().to_netcdf(cache/'pressure.nc', engine='scipy')
        metadata = {'item': {'group': 'pressure', 'dataset': 'reanalysis-era5-pressure-levels',
                            'time': STAMP, 'request': {'area': [71., 29., 69., 31.]}}}
        (cache/'pressure.json').write_text(json.dumps(metadata), encoding='utf-8')
        self.assertEqual(self.app.resolve_auto_profile(70., 30., STAMP)['valid_time'], STAMP)
        self.assertIsNone(self.app.resolve_auto_profile(70., 30., '2024-01-03T00:00:00Z'))
        metadata['item']['time'] = '2024-01-01T03:00:00Z'
        (cache/'pressure.json').write_text(json.dumps(metadata), encoding='utf-8')
        self.assertIsNone(self.app.resolve_auto_profile(70., 30., '2024-01-01T03:00:00Z'))
