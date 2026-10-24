"""Software contract tests for attributed satellite mask import; no accuracy claims."""
import hashlib
from pathlib import Path
import tempfile
import unittest

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from arktika.workstation import Workstation


def geotiff(values, *, crs='EPSG:3413', transform=None, nodata=None, mask=None):
    values=np.asarray(values)
    if transform is None:transform=from_origin(200000,-1000000,25000,25000)
    with MemoryFile() as memory:
        with memory.open(driver='GTiff',width=values.shape[1],height=values.shape[0],
                         count=1,dtype=values.dtype,crs=crs,transform=transform,nodata=nodata) as dst:
            dst.write(values,1)
            if mask is not None:dst.write_mask(np.asarray(mask,dtype=np.uint8))
        return memory.read()


class PmcObservationIngest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=Workstation(self.tmp.name)
        self.labels=np.array([[0,1,0],[0,0,1]],dtype=np.uint8)
        self.coverage=np.array([[1,1,0],[1,0,1]],dtype=np.uint8)
        self.transform=from_origin(200000,-1000000,25000,25000)
        self.labels_bytes=geotiff(self.labels,transform=self.transform)
        self.coverage_bytes=geotiff(self.coverage,transform=self.transform)
        self.metadata={
            'schema_version':'arktika.pmc_satellite_observation.v1',
            'observation_ref':'scene-20240101-a',
            'source':'satellite-provider',
            'product':'cloud-mask',
            'product_version':'1.2',
            'valid_time':'2024-01-01T00:00:00Z',
            'label_convention':'0=no_pmc,1=pmc_candidate',
            'qc_status':'not_assessed',
            'grid':{'crs':'EPSG:3413','width':3,'height':2,
                    'transform':list(self.transform[:6]),
                    'resolution_x_m':25000.0,'resolution_y_m':25000.0},
            'nodata':None,
        }

    def tearDown(self):
        self.app.close();self.tmp.cleanup()

    def register(self,**kwargs):
        return self.app.register_pmc_observation(kwargs.get('labels_bytes',self.labels_bytes),
                    kwargs.get('coverage_bytes',self.coverage_bytes),kwargs.get('metadata',self.metadata))

    def test_register_and_reload_masks_with_verified_private_archive(self):
        result=self.register()
        self.assertRegex(result['observation_id'],r'^[0-9a-f]{24}$')
        self.assertEqual(result['status'],'imported_unverified')
        self.assertEqual(result['observation']['valid_time'],'2024-01-01T00:00:00Z')
        self.assertEqual(result['observation']['grid'],self.metadata['grid'])
        self.assertEqual(result['sha256'],hashlib.sha256(
            (self.app.fields_root/'pmc-observations'/(result['observation_id']+'.npz')).read_bytes()).hexdigest())
        labels,coverage,metadata=self.app.pmc_observation(result['observation_id'])
        np.testing.assert_array_equal(labels,self.labels)
        np.testing.assert_array_equal(coverage,self.coverage)
        self.assertEqual(metadata['observation_id'],result['observation_id'])
        self.assertEqual(metadata['status'],'imported_unverified')
        state=self.app.pmc_observations_state()
        self.assertEqual([item['observation_id'] for item in state['observations']],[result['observation_id']])
        self.assertNotIn('path',state['observations'][0])

    def test_preview_reports_coverage_counts_without_creating_storage(self):
        result=self.app.preview_pmc_observation(
            self.labels_bytes,self.coverage_bytes,self.metadata)
        self.assertEqual(result['status'],'preview_only')
        self.assertEqual(result['ingestion_status'],'not_ingested')
        self.assertNotIn('status',result['observation'])
        summary=result['structural_summary']
        self.assertEqual(summary['total_pixels'],6)
        self.assertEqual(summary['covered_pixels'],4)
        self.assertEqual(summary['unknown_pixels'],2)
        self.assertEqual(summary['positive_label_pixels_within_coverage'],2)
        self.assertEqual(summary['coverage_fraction'],4/6)
        self.assertRegex(result['source_package_sha256'],r'^[0-9a-f]{64}$')
        self.assertFalse((Path(self.tmp.name)/'reanalysis'/'pmc-observations').exists())
        self.assertIn('метеорологической оценкой',result['meaning'])

    def test_preview_package_hash_includes_exact_sidecar_bytes(self):
        first=self.app.preview_pmc_observation(
            self.labels_bytes,self.coverage_bytes,self.metadata,
            metadata_source_bytes=b'{"source":"satellite-provider"}')
        second=self.app.preview_pmc_observation(
            self.labels_bytes,self.coverage_bytes,self.metadata,
            metadata_source_bytes=b'{ "source" : "satellite-provider" }')
        self.assertNotEqual(first['source_package_sha256'],second['source_package_sha256'])

    def test_preview_rejects_invalid_inputs_with_same_validation_as_import(self):
        with self.assertRaisesRegex(ValueError,'GeoTIFF'):
            self.app.preview_pmc_observation(b'<html>denied</html>',
                self.coverage_bytes,self.metadata)
        bad={**self.metadata,'label_convention':'1=ice'}
        with self.assertRaisesRegex(ValueError,'Соглашение меток'):
            self.app.preview_pmc_observation(self.labels_bytes,
                self.coverage_bytes,bad)
        self.assertFalse((Path(self.tmp.name)/'reanalysis'/'pmc-observations').exists())

    def test_exact_time_observation_is_reprojected_for_archive_grid(self):
        self.register()
        target = {'crs':'EPSG:3413','width':6,'height':4,
                  'transform':[12500,0,200000,0,-12500,-1000000],
                  'resolution_x_m':12500.0,'resolution_y_m':12500.0}
        observation = self.app._pmc_observation_for_frame(self.metadata['valid_time'], target)
        self.assertEqual(observation['metadata']['regridding']['method'],'nearest_neighbor')
        self.assertEqual(observation['metadata']['regridding']['source_grid']['width'],3)
        self.assertEqual(observation['metadata']['grid'],target)
        self.assertEqual(observation['labels'].shape,(4,6))
        self.assertEqual(observation['coverage'].shape,(4,6))

    def test_duplicate_exact_time_observations_have_safe_unknown_reason(self):
        self.register()
        self.register(metadata={**self.metadata,'observation_ref':'scene-20240101-b'})
        result = self.app._pmc_observation_for_frame(
            self.metadata['valid_time'], self.metadata['grid'])
        self.assertEqual(result,{'unavailable_reason':'duplicate_exact_time_observations'})

    def test_optional_scan_interval_round_trips_as_utc_and_contains_nominal_time(self):
        metadata={**self.metadata,
                  'scan_start_utc':'2024-01-01T00:00:00+00:00',
                  'scan_end_utc':'2024-01-01T00:30:00Z'}
        result=self.register(metadata=metadata)
        _,_,saved=self.app.pmc_observation(result['observation_id'])
        self.assertEqual('2024-01-01T00:00:00Z',saved['scan_start_utc'])
        self.assertEqual('2024-01-01T00:30:00Z',saved['scan_end_utc'])

    def test_scan_interval_must_be_a_complete_ordered_utc_pair_containing_valid_time(self):
        invalid_intervals=(
            {'scan_start_utc':'2024-01-01T00:00:00Z'},
            {'scan_end_utc':'2024-01-01T00:30:00Z'},
            {'scan_start_utc':'2024-01-01T00:00:00',
             'scan_end_utc':'2024-01-01T00:30:00Z'},
            {'scan_start_utc':'2024-01-01T00:00:00+01:00',
             'scan_end_utc':'2024-01-01T00:30:00Z'},
            {'scan_start_utc':'2024-01-01T00:30:00Z',
             'scan_end_utc':'2024-01-01T00:00:00Z'},
            {'scan_start_utc':'2024-01-01T00:30:01Z',
             'scan_end_utc':'2024-01-01T01:00:00Z'},
        )
        for interval in invalid_intervals:
            with self.subTest(interval=interval),self.assertRaises(ValueError):
                self.register(metadata={**self.metadata,**interval})

    def test_archive_frame_uses_one_unique_scan_interval_including_closed_endpoint(self):
        metadata={**self.metadata,
                  'valid_time':'2024-01-01T00:30:00Z',
                  'scan_start_utc':'2024-01-01T00:30:00Z',
                  'scan_end_utc':'2024-01-01T01:00:00Z'}
        self.register(metadata=metadata)
        observation=self.app._pmc_observation_for_frame(
            '2024-01-01T01:00:00Z',self.metadata['grid'])
        self.assertEqual('2024-01-01T00:30:00Z',observation['metadata']['valid_time'])
        self.assertEqual('2024-01-01T00:30:00Z',observation['metadata']['scan_start_utc'])
        self.assertEqual('2024-01-01T01:00:00Z',observation['metadata']['scan_end_utc'])

    def test_no_covering_scan_is_unknown_and_two_covering_scans_are_ambiguous(self):
        no_coverage={**self.metadata,'valid_time':'2024-01-01T00:15:00Z',
                     'scan_start_utc':'2024-01-01T00:00:00Z',
                     'scan_end_utc':'2024-01-01T00:30:00Z'}
        self.register(metadata=no_coverage)
        uncovered=self.app._pmc_observation_for_frame(
            '2024-01-01T01:00:00Z',self.metadata['grid'])
        self.assertEqual('no_eligible_observation',uncovered['unavailable_reason'])

        first={**self.metadata,'valid_time':'2024-01-01T00:45:00Z',
               'scan_start_utc':'2024-01-01T00:30:00Z',
               'scan_end_utc':'2024-01-01T01:15:00Z'}
        second={**self.metadata,'observation_ref':'scene-20240101-b',
                'valid_time':'2024-01-01T01:10:00Z',
                'scan_start_utc':'2024-01-01T00:50:00Z',
                'scan_end_utc':'2024-01-01T01:20:00Z'}
        self.register(metadata=first)
        self.register(metadata=second)
        ambiguous=self.app._pmc_observation_for_frame(
            '2024-01-01T01:00:00Z',self.metadata['grid'])
        self.assertEqual('ambiguous_eligible_observations',ambiguous['unavailable_reason'])

    def test_html_xml_and_malformed_inputs_are_not_accepted_as_geotiff(self):
        for payload in (b'<html>denied</html>',b'<?xml version="1.0"?><Error/>',b'not a raster'):
            with self.subTest(payload=payload),self.assertRaisesRegex(ValueError,'GeoTIFF'):
                self.register(labels_bytes=payload)

    def test_binary_class_values_and_single_band_are_required(self):
        bad=geotiff(np.array([[0,2,0],[0,0,1]],dtype=np.uint8),transform=self.transform)
        with self.assertRaisesRegex(ValueError,'только значения 0 и 1'):
            self.register(labels_bytes=bad)
        with self.assertRaisesRegex(ValueError,'nodata'):
            self.register(labels_bytes=geotiff(self.labels,transform=self.transform,nodata=255))

    def test_internal_geotiff_masks_are_rejected_instead_of_counted_as_clear(self):
        validity=np.full(self.labels.shape,255,dtype=np.uint8);validity[0,0]=0
        masked=geotiff(self.labels,transform=self.transform,mask=validity)
        with self.assertRaisesRegex(ValueError,'внутренние маски'):
            self.register(labels_bytes=masked)

    def test_masks_and_declared_grid_must_match_exactly(self):
        shifted=geotiff(self.coverage,transform=from_origin(21,80,0.25,0.25))
        with self.assertRaisesRegex(ValueError,'одинаковые сетку и CRS'):
            self.register(coverage_bytes=shifted)
        bad=dict(self.metadata);bad['grid']=dict(self.metadata['grid'],width=4)
        with self.assertRaisesRegex(ValueError,'совпадать'):
            self.register(metadata=bad)

    def test_sidecar_requires_explicit_utc_provenance_and_no_private_extras(self):
        for key,value in (('valid_time','2024-01-01T00:00:00'),('source','https://private.example/x'),
                          ('label_convention','1=ice'),('qc_status','passed')):
            bad=dict(self.metadata);bad[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.register(metadata=bad)
        bad=dict(self.metadata,private_uri='s3://secret/object')
        with self.assertRaisesRegex(ValueError,'ровно определённые поля'):
            self.register(metadata=bad)

    def test_saved_archive_tampering_is_detected(self):
        result=self.register();identity=result['observation_id']
        path=self.app.fields_root/'pmc-observations'/(identity+'.npz')
        path.write_bytes(path.read_bytes()+b'x')
        with self.assertRaisesRegex(ValueError,'Контрольная сумма'):
            self.app.pmc_observation(identity)


if __name__=='__main__':unittest.main()
