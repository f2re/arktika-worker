"""Проверки STAC на вымышленных метаданных и контрольных суммах."""
import copy
import hashlib
import json
import unittest

from arktika.gptl_native import parse_native_item


DATA = '1' * 64
QC = '2' * 64
EXTENSION = 'https://stac-extensions.github.io/timestamps/v1.1.0/schema.json'


def fixture(platform='EL3'):
    return {
        'type': 'Feature',
        'id': f'ETRIS.{platform}.2024-01-02T2300.L2IR',
        'properties': {
            'platform': platform, 'processing:level': 'L2IR',
            'datetime': '2024-01-02T23:00:00.025Z',
            'start_datetime': '2024-01-03T01:58:00+03:00',
            'end_datetime': '2024-01-02T23:02:00Z',
            'ready_at': '2024-01-02T23:04:00Z',
            'published': '2024-01-02T23:20:00Z',
        },
        'stac_extensions': [EXTENSION],
        'assets': {
            'msugs.ir.4326.09': {
                'href': f'https://s3.gptl.ru/sample/{platform}_ch9.tif?versionId=fixed-data',
                'channel': 9, 'file:size': 12, 'file:checksum': '1220' + DATA,
                'raster:bands': [{'unit': 'K', 'scale': 0.01, 'offset': 100., 'nodata': 0}],
                'calibration:version': 'fixture v1',
                'published': '2024-01-02T23:05:00Z',
            },
            'qc': {
                'href': f'https://s3.gptl.ru/sample/{platform}_qc.tif?versionId=fixed-quality',
                'roles': ['quality-mask'], 'file:checksum': QC,
                'classification:classes': [{'value': 0, 'name': 'good'}, {'value': 1, 'name': 'invalid'}],
                'published': '2024-01-02T23:07:00Z',
            },
        },
    }


def raw_bytes(item):
    return json.dumps(item, indent=2).encode()


def parse(item, **kwargs):
    return parse_native_item(raw_bytes(item), 'msugs.ir.4326.09', 'qc', **kwargs)


class NativeGptlTest(unittest.TestCase):
    def test_historical_both_families_preserve_raw_and_compute_latest_asset_time(self):
        for platform, instrument in (('ARCM1', 'MSU-GS/A'), ('ARCM2', 'MSU-GS/A'),
                                     ('EL2', 'MSU-GS'), ('EL3', 'MSU-GS'), ('EL4', 'MSU-GS')):
            with self.subTest(platform=platform):
                item = fixture(platform)
                raw = raw_bytes(item)
                plan = parse_native_item(raw, 'msugs.ir.4326.09', 'qc', historical=True)
                self.assertEqual(plan['producer_item_sha256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual(plan['scene_id'], item['id'])
                self.assertEqual(plan['platform'], platform)
                self.assertEqual(plan['instrument'], instrument)
                self.assertEqual(plan['observed_at'], '2024-01-02T23:00:00.025000Z')
                self.assertEqual(plan['raster_asset']['time'], plan['observed_at'])
                self.assertEqual(plan['scan_start'], '2024-01-02T22:58:00Z')
                self.assertEqual(plan['provider_ready_at'], '2024-01-02T23:04:00Z')
                self.assertEqual(plan['available_at'], '2024-01-02T23:07:00Z')
                self.assertEqual(plan['raster_asset']['provider_sha256'], DATA)
                self.assertEqual(plan['quality_asset']['provider_sha256'], QC)
                self.assertEqual(plan['raster_asset']['object_version'], 'fixed-data')
                self.assertEqual(plan['quality_asset']['object_version'], 'fixed-quality')
                self.assertNotEqual(plan['raster_asset']['id'], plan['quality_asset']['id'])
                href = item['assets']['msugs.ir.4326.09']['href']
                legacy_id = hashlib.sha256((item['id'] + '\n' + href.split('?')[0]).encode()).hexdigest()[:32]
                self.assertEqual(plan['raster_asset']['id'], legacy_id)
                self.assertEqual(plan['raster_asset']['uri'], href)
                self.assertEqual(plan['quality']['good_flag'], 0)
                self.assertEqual(plan['calibration']['unit'], 'K')
                self.assertEqual(plan['calibration']['revision'], 'fixture v1')

    def test_prospective_waits_for_local_receipts_without_inventing_provider_times(self):
        item = fixture()
        del item['properties']['ready_at']
        del item['stac_extensions']
        for asset in item['assets'].values():
            del asset['published']
            del asset['file:checksum']
            asset['href'] = asset['href'].split('?')[0].replace('s3.gptl.ru', 's3ext.gptl.ru')
        plan = parse(item)
        for key in ('available_at', 'provider_ready_at', 'raster_published_at', 'quality_published_at'):
            self.assertIsNone(plan[key])
        self.assertIsNone(plan['raster_asset']['provider_sha256'])
        self.assertIsNone(plan['quality_asset']['object_version'])
        with self.assertRaises(ValueError):
            parse(item, historical=True)

    def test_rejects_unknown_or_unusable_scan_bounds(self):
        for key, value in (('start_datetime', None), ('end_datetime', '2024-01-02T23:02:00'),
                           ('datetime', '2024-01-02T23:00:00-00:00'),
                           ('start_datetime', '2024-01-02T22:58:00+00:99'),
                           ('end_datetime', '2024-01-02T22:58:00Z'),
                           ('ready_at', '2024-01-02T23:00:00Z')):
            with self.subTest(key=key, value=value):
                item = fixture()
                item['properties'][key] = value
                with self.assertRaises(ValueError):
                    parse(item)

    def test_rejects_ambiguous_or_ineligible_thermal_data(self):
        item = fixture()
        raster = item['assets']['msugs.ir.4326.09']
        for field, invalid in (('unit', 'DN'), ('scale', False), ('scale', -1),
                               ('offset', '+100'), ('nodata', 'auto')):
            with self.subTest(field=field, invalid=invalid):
                mutated = copy.deepcopy(item)
                mutated['assets']['msugs.ir.4326.09']['raster:bands'][0][field] = invalid
                with self.assertRaises(ValueError):
                    parse(mutated)
        raster['channel'] = 6
        with self.assertRaisesRegex(ValueError, 'канала'):
            parse(item)

    def test_rejects_unproven_or_ambiguous_qc(self):
        for invalid in (
            {'roles': ['visual']},
            {'classification:classes': [{'value': 1, 'name': 'good'}]},
            {'classification:classes': [{'value': 0, 'name': 'valid'}, {'value': 0, 'name': 'invalid'}]},
            {'classification:bitfields': [{'offset': 0}]},
            {'channel': 5},
        ):
            with self.subTest(invalid=invalid):
                item = fixture()
                item['assets']['qc'].update(invalid)
                with self.assertRaises(ValueError):
                    parse(item)
        item = fixture()
        classes = item['assets']['qc'].pop('classification:classes')
        item['assets']['qc']['raster:bands'] = [{'classification:classes': classes}]
        self.assertEqual(parse(item)['quality']['classes'], classes)

    def test_generic_qc_requires_channel_mapping_with_multiple_numeric_assets(self):
        item = fixture()
        other = copy.deepcopy(item['assets']['msugs.ir.4326.09'])
        other.update(channel=8, href=other['href'].replace('_ch9', '_ch8'))
        item['assets']['msugs.ir.4326.08'] = other
        with self.assertRaisesRegex(ValueError, 'явная связь QC'):
            parse(item)
        item['assets']['qc']['channel'] = 9
        self.assertEqual(parse(item)['quality_asset']['channel'], 9)

    def test_rejects_unverified_archive_release(self):
        for key, value in (('published', None), ('file:checksum', None),
                           ('published', '2024-01-02T22:55:00Z'),
                           ('ready_at', '2024-01-02T23:09:00Z'),
                           ('created', '2024-01-03T00:09:00Z'),
                           ('updated', '2024-01-03T00:09:00Z'),
                           ('unpublished', '2024-01-03T00:09:00Z')):
            with self.subTest(key=key, value=value):
                item = fixture()
                item['assets']['qc'][key] = value
                with self.assertRaises(ValueError):
                    parse(item, historical=True)

    def test_rejects_untrusted_or_mutable_archive_objects(self):
        for invalid in (
            'https://other.invalid/bucket/file.tif?versionId=abc',
            'https://s3.gptl.ru/path/to/file.jpg?versionId=abc',
            'https://s3.gptl.ru/path/to/file.tif',
            'https://s3.gptl.ru/path/file.tif?versionId=a&versionId=b',
            'https://s3.gptl.ru/path/to/file.tif?versionId=null',
            'https://s3.gptl.ru/path/to/file.tif?versionId=abc&X-Amz-Signature=secret',
            'https://s3.gptl.ru/sample/%2e%2e/file.tif?versionId=abc',
        ):
            with self.subTest(invalid=invalid):
                item = fixture()
                item['assets']['qc']['href'] = invalid
                with self.assertRaises(ValueError):
                    parse(item, historical=True)
        item = fixture()
        item['assets']['qc']['href'] = item['assets']['msugs.ir.4326.09']['href']
        with self.assertRaisesRegex(ValueError, 'разными'):
            parse(item, historical=True)

    def test_duplicate_keys_and_nonfinite_json_are_rejected(self):
        for raw in (b'{"id":"one","id":"two"}', b'{"properties":{"offset":NaN}}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_native_item(raw, 'raster', 'quality')


if __name__ == '__main__':
    unittest.main()
