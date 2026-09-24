"""Regression cases for release 0.2.2. All inputs here are synthetic software fixtures."""
import concurrent.futures
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from arktika.auth import AuthClient
from arktika.download import atomic_json, download, object_validator
from arktika.interpretation import normalize_profile, parse_profile, phase_field, profile_diagnostics, spectral
from arktika.network import Client, NetworkError, Response, SECRETS, redact
from arktika.processing import CalibrationError, build_product, calibration, validate_calibration
from arktika.profiles import cloud_top, integrate
from arktika.workstation import Workstation
from test_interpretation import pixel, profile
from test_science import fixture
from test_transport import DATA, FakeTransport, Raw, asset


class CalibrationRegressions(unittest.TestCase):
    def declared(self, channel='9', **changes):
        coefficients = dict(units='K', scale=1, offset=0)
        coefficients.update(changes)
        return dict(mode='declared', reference='SYNTHETIC TEST CONTRACT', channels={channel: coefficients})

    def test_not_a_dictionary(self):
        for value in (None, [], 'assumed'):
            with self.subTest(value=value), self.assertRaises(CalibrationError):
                validate_calibration(value)

    def test_invalid_coefficients_are_rejected(self):
        for changes in ({'scale': 0}, {'scale': -1}, {'scale': True}, {'offset': False}, {'offset': float('inf')}):
            with self.subTest(changes=changes), self.assertRaises(CalibrationError):
                validate_calibration(self.declared(**changes))

    def test_visible_temperature_contract_rejected(self):
        with self.assertRaises(CalibrationError):
            validate_calibration(self.declared('1'))

    def test_visible_bands_never_assumed_kelvin(self):
        for band in (1, 2, 3):
            self.assertEqual(calibration(None, band, {'mode': 'assumed'})[2], 'DN')

    def test_bad_metadata_scale_rejected(self):
        ds = SimpleNamespace(units=['K'], scales=[-1], offsets=[0])
        with self.assertRaises(CalibrationError):
            calibration(ds, 9, {'mode': 'unknown'})

    def test_visible_pixel_not_a_thermal_measurement(self):
        result = spectral(pixel({1: 270}, unit='K'))
        self.assertFalse(result['quality_ok'])
        self.assertEqual(result['metrics'], [])

    def test_metadata_origin_is_preserved(self):
        p = pixel({9: 270})
        p['channels'][0]['calibration'] = 'metadata'
        self.assertEqual(spectral(p)['calibration'], 'metadata')

    def test_no_nan_phase_class_when_common_mask_is_wrong(self):
        arrays = {7: np.array([[np.nan]]), 9: np.array([[260.]]), 10: np.array([[259.]])}
        self.assertEqual(phase_field(arrays, np.ones((1, 1), bool))[0, 0], 0)

    def test_assumed_metrics_not_reported_as_measurements(self):
        self.assertTrue(all(m['kind'] in ('assumption', 'derived') for m in spectral(pixel())['metrics']))


class ProfileRegressions(unittest.TestCase):
    def test_missing_solution_not_ambiguity(self):
        result = cloud_top(dict(cloud_confirmed=True, temperature_units='K', height_units='m',
                                height=[0, 1000], temperature=[280, 270], brightness_temperature=250))
        self.assertFalse(result['has_solution'])
        self.assertFalse(result['ambiguous'])
        self.assertEqual(result['candidate_heights_m'], [])

    def test_nonfinite_cloud_top_profile_rejected(self):
        with self.assertRaises(ValueError):
            cloud_top(dict(cloud_confirmed=True, temperature_units='K', height_units='m',
                           height=[0, 1000], temperature=[280, float('nan')], brightness_temperature=275))

    def test_nonstr_source_rejected(self):
        for value in (True, 42, {'x': 'y'}, '  '):
            with self.subTest(value=value), self.assertRaises(ValueError):
                profile(source=value)

    def test_malformed_csv_not_silently_repaired(self):
        texts = ['height_m,temperature_k,temperature_k\n0,280,280\n1000,270,270',
                 'height_m,temperature_k\n0,280,1\n1000,270',
                 'height_m,temperature_k\n0,280\n1000']
        metadata = dict(source='TEST', valid_time='2026-01-01T00:00:23Z', lon=30, lat=70)
        for text in texts:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_profile({'text': text, 'metadata': metadata})

    def test_profile_seconds_are_retained(self):
        self.assertEqual(profile(valid_time='2026-01-01T00:00:23Z')['valid_time'], '2026-01-01T00:00:23Z')

    def test_integral_condensate_requires_units(self):
        data = dict(pressure=[90000, 100000], specific_humidity=[.001, .002], pressure_units='Pa',
                    humidity_units='kg/kg', cloud_liquid=[.0001, .0001], source='TEST', valid_time='2026-01-01T00:00:00Z')
        with self.assertRaises(ValueError):
            integrate(data)

    def test_wind_cannot_be_half_present(self):
        data = dict(pressure=[90000, 100000], specific_humidity=[.001, .002], pressure_units='Pa',
                    humidity_units='kg/kg', u=[1, 1], wind_units='m/s', source='TEST', valid_time='2026-01-01T00:00:00Z')
        with self.assertRaises(ValueError):
            integrate(data)

    def test_layer_does_not_invent_droplet_size_or_severity(self):
        p = pixel()
        result = profile_diagnostics(profile(), p, spectral(p), 2000)
        self.assertTrue(result['layer']['icing_conditions'])
        self.assertFalse(result['layer']['droplet_size_available'])
        self.assertEqual(result['layer']['assessment_type'], 'profile_thermodynamics')
        self.assertNotIn('severity', result['layer'])


class TransportRegressions(unittest.TestCase):
    def test_manual_token_change_drops_old_account_refresh(self):
        client = AuthClient('old-test-token')
        client.replace('old-test-token', 'old-test-refresh')
        client.pending = {'pending-test-state': ('verifier', 10**15)}
        client.set_token('new-test-token')
        self.assertEqual(client.refresh_token, '')
        self.assertEqual(client.pending, {})
        with self.assertRaises(ValueError):
            client.refresh(force=True)

    def test_redact_longest_secret_first(self):
        old = list(SECRETS)
        try:
            SECRETS.clear(); SECRETS.extend(['test-secret', 'test-secret-long'])
            self.assertNotIn('-long', redact('test-secret-long'))
        finally:
            SECRETS.clear(); SECRETS.extend(old)

    def test_atomic_json_rejects_non_json_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            atomic_json(path, {'value': 1})
            with self.assertRaises(ValueError):
                atomic_json(path, {'value': float('nan')})
            self.assertEqual(json.loads(path.read_text()), {'value': 1})
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_concurrent_json_writes_do_not_share_temp_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda n: atomic_json(path, {'writer': n, 'body': 'x' * 1000}), range(32)))
            self.assertIn(json.loads(path.read_text())['writer'], range(32))
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_weak_etag_not_used_for_byte_identity(self):
        self.assertEqual(object_validator({'etag': 'W/"test"'}), ('', ''))
        self.assertEqual(object_validator({'etag': '"test"'}), ('etag', '"test"'))

    def test_valid_last_modified_fallback(self):
        value = 'Mon, 21 Sep 2026 00:00:00 GMT'
        self.assertEqual(object_validator({'etag': 'W/"test"', 'last_modified': value, 'response_date': 'Mon, 21 Sep 2026 00:02:00 GMT'}), ('last_modified', value))
        self.assertEqual(object_validator({'last_modified': 'not-a-date'}), ('', ''))
        self.assertEqual(object_validator({'last_modified': value}), ('', ''))
        self.assertEqual(object_validator({'last_modified': value, 'response_date': value}), ('', ''))

    def test_multipart_weak_etag_refused_before_partial_write(self):
        class Weak(FakeTransport):
            def open(self, *args, **kwargs):
                response = super().open(*args, **kwargs)
                response.headers['etag'] = 'W/"fixture"'
                return response
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'sample.tif'
            with self.assertRaises(NetworkError):
                download(Client(transport=Weak()), asset(), target, lambda *args: None, threading.Event(), chunk_size=2048)
            self.assertFalse(target.exists())
            self.assertFalse(Path(str(target) + '.part').exists())

    def test_weak_etag_complete_single_response_is_allowed(self):
        class Complete(FakeTransport):
            def open(self, *args, **kwargs):
                return Response(Raw(200, DATA, {'Content-Length': str(len(DATA)), 'ETag': 'W/"fixture"', 'Content-Type': 'image/tiff'}))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'sample.tif'
            download(Client(transport=Complete()), asset(), target, lambda *args: None, threading.Event())
            self.assertEqual(target.read_bytes(), DATA)


class MotionRegressions(unittest.TestCase):
    def test_forward_backward_translation_error(self):
        from arktika.motion import block_match
        rng = np.random.default_rng(212)
        a = rng.normal(size=(96,96))
        b = np.roll(np.roll(a,2,axis=0),-3,axis=1)
        vectors = block_match(a,b)
        self.assertGreater(len(vectors), 3)
        self.assertTrue(all(v['forward_backward_error_px']==0 for v in vectors))
        self.assertTrue(all((v['dx'],v['dy'])==(-3,2) for v in vectors))

    def test_motion_can_cancel_during_tracking(self):
        from arktika.motion import block_match
        from arktika.network import Cancelled
        event=threading.Event(); event.set()
        with self.assertRaises(Cancelled):
            block_match(np.ones((64,64)), np.ones((64,64)), cancel=event)

    def test_motion_bad_patch_rejected(self):
        from arktika.motion import block_match
        with self.assertRaises(ValueError):
            block_match(np.ones((64,64)), np.ones((64,64)), patch=15)


class SpatialProvenanceRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scene = fixture(self.root)
        self.app = Workstation(self.root / 'state')
        self.app.scan_local(self.root)
        self.product = build_product(self.scene, self.app.product_root,
                                     dict(product='channel', preset='barents', width=256, channel=9), {'mode': 'unknown'})

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_exact_right_bottom_edges_are_outside(self):
        grid = self.app.product_grid(self.product)
        for x, y in ((grid['width'], 10), (10, grid['height']), (-.1, 10)):
            with self.subTest(x=x, y=y), self.assertRaises(ValueError):
                self.app.coordinates(dict(product=self.product['id'], x=x, y=y))

    def test_product_units_from_metadata_not_user_declaration(self):
        fixture(self.root, units=True)
        result = build_product(self.scene, self.app.product_root,
                               dict(product='channel', preset='barents', width=256, channel=9), {'mode': 'unknown'})
        self.assertEqual(result['calibration_status'], 'metadata')

    def test_route_keeps_full_profile_and_channel_inputs(self):
        p = self.app.import_profile(profile())
        result = self.app.route(dict(product=self.product['id'], points=[[30, 70], [31, 70]],
                                     profile_id=p['id'], altitude_m=2000, departure='2026-01-01T00:00:00Z'))
        self.assertEqual(result['profile_snapshot']['id'], p['id'])
        self.assertEqual(len(result['inputs_all']), 7)
        self.assertEqual(result['method_version'], 'spectral-rules-0.2.2')

    def test_rules_version_changes_product_cache_identity(self):
        with patch('arktika.interpretation.VERSION', 'regression-different-method'):
            another = build_product(self.scene, self.app.product_root,
                                     dict(product='channel', preset='barents', width=256, channel=9), {'mode': 'unknown'})
        self.assertNotEqual(self.product['id'], another['id'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
