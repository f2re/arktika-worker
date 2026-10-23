"""Software contract tests for attributed satellite mask import; no accuracy claims."""
import hashlib
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
