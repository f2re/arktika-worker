"""Local HTTP contract for the single-field experimental PMC endpoint."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from server import LocalServer
from arktika.workstation import Workstation


class PmcRunHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=Workstation(Path(self.temp.name)/'state')
        self.server=LocalServer(('127.0.0.1',0),self.app,key='pmc-test')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_address[1])
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.grid={'crs':'EPSG:3413','width':4,'height':3,
                   'transform':[1000,0,-2000,0,-1000,5000],
                   'resolution_x_m':1000,'resolution_y_m':1000}

    def tearDown(self):
        self.server.shutdown();self.thread.join(3);self.server.server_close()
        self.app.close();self.temp.cleanup()

    def request(self,data):
        req=urllib.request.Request(self.url+'/api/pmc/run',
            headers={'Cookie':'arktika_session=pmc-test','Content-Type':'application/json',
                     'X-Arktika-Request':'1'},data=json.dumps(data).encode())
        return self.opener.open(req,timeout=5)

    def multipart(self,parts):
        boundary='arktika-test-boundary'
        chunks=[]
        for name,filename,payload in parts:
            chunks.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
                           b'Content-Type: application/octet-stream\r\n\r\n',payload,b'\r\n'))
        chunks.append(f'--{boundary}--\r\n'.encode());body=b''.join(chunks)
        req=urllib.request.Request(self.url+'/api/pmc/observations/import',data=body,
            headers={'Cookie':'arktika_session=pmc-test','Content-Type':f'multipart/form-data; boundary={boundary}',
                     'X-Arktika-Request':'1'})
        return self.opener.open(req,timeout=5)

    def test_endpoint_requires_explicit_cached_field_and_target_grid(self):
        for payload in ({'target_grid':self.grid},
                        {'field_id':'wind-1'},
                        {'field_id':'wind-1','target_grid':self.grid,'source':'era5'}):
            with self.subTest(payload=payload), self.assertRaises(urllib.error.HTTPError) as cm:
                self.request(payload)
            self.assertEqual(cm.exception.code,400)

    def test_endpoint_limits_target_grid_size_before_computing(self):
        grid=dict(self.grid,width=2048,height=1024)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.request({'field_id':'wind-1','target_grid':grid})
        self.assertEqual(cm.exception.code,400)

    def test_success_returns_experimental_status_provenance_and_projected_centres(self):
        fake={
            'algorithm_id':'arktika.experimental_pmc_candidates','algorithm_version':'0.1.1',
            'maturity':'experimental','source':'era5','valid_time':'2024-01-01T00:00:00Z',
            'candidate_count':1,'candidates':[{'candidate_id':'pmc-candidate-0001',
                'classification':'candidate','confirmation_status':'not_confirmed','pixel_count':3,
                'peak_850hpa_relative_vorticity_s-1':0.00013,'peak_pixel_rc':[1,2],
                'centroid_pixel_rc':[1.0,1.5]}],
            'grid':{'crs':'EPSG:3413','width':4,'height':3,
                    'transform':[1000,0,-2000,0,-1000,5000],
                    'resolution_x_m':1000,'resolution_y_m':1000},
            'field_passport':{'source':'era5','algorithm_id':'arktika.experimental_relative_vorticity_850'},
            'passport':{'algorithm':{'id':'arktika.experimental_pmc_candidates','version':'0.1.1'},
                        'quantity_kind':'candidate_catalog'},
            'input_coverage':{'valid_pixels':10,'total_pixels':12,'valid_fraction':10/12},
            'satellite_comparison':{'status':'not_supplied','metrics':None},
            'parameters':{'gaussian_sigma_km':60},'limitations':['Кандидат не подтверждён.']}
        with patch.object(self.app,'experimental_pmc_candidates',return_value=fake) as run:
            with self.request({'field_id':'explicit-cache-id','target_grid':self.grid}) as response:
                result=json.loads(response.read())
        run.assert_called_once_with('explicit-cache-id',target_grid=self.grid)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['maturity'],'experimental')
        self.assertEqual(result['candidate_count'],1)
        self.assertEqual(result['field_passport']['source'],'era5')
        self.assertEqual(result['detector_passport']['algorithm']['id'],
                         'arktika.experimental_pmc_candidates')
        candidate=result['candidates'][0]
        self.assertEqual(candidate['peak_xy_m'],[500,3500])
        self.assertEqual(candidate['centroid_xy_m'],[0,3500])
        self.assertEqual(result['satellite_comparison']['status'],'not_supplied')

    def test_route_does_not_bypass_cached_source_validation(self):
        # The route delegates only the selected ID to Workstation's checksum,
        # source, level, time, unit and grid validators; unsupported fields fail closed.
        with patch.object(self.app,'experimental_pmc_candidates',
                          side_effect=ValueError('Экспериментальная вихревость поддерживает только ERA5 и MERRA-2.')) as run:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.request({'field_id':'carra-field','target_grid':self.grid})
            self.assertEqual(cm.exception.code,400)
            run.assert_called_once()

    def test_optional_observation_id_is_loaded_and_passed_with_coverage(self):
        import numpy as np
        fake={
            'algorithm_id':'arktika.experimental_pmc_candidates','algorithm_version':'0.1.1',
            'maturity':'experimental','source':'era5','valid_time':'2024-01-01T00:00:00Z',
            'candidate_count':0,'candidates':[],'grid':self.grid,
            'field_passport':{'source':'era5'},'input_coverage':{'valid_pixels':12},
            'passport':{'algorithm':{'id':'arktika.experimental_pmc_candidates','version':'0.1.1'},
                        'quantity_kind':'candidate_catalog'},
            'satellite_comparison':{'status':'comparison_only','metrics':None},
            'parameters':{},'limitations':['Метеорологическая точность не проверялась.']}
        labels=np.zeros((3,4),dtype=np.uint8);coverage=np.ones((3,4),dtype=np.uint8)
        metadata={'source':'test','product_id':'mask-v1','observation_id':'a'*24,
                  'valid_time':'2024-01-01T00:00:00Z','grid':self.grid}
        with patch.object(self.app,'pmc_observation',return_value=(labels,coverage,metadata)) as load, \
             patch.object(self.app,'experimental_pmc_candidates',return_value=fake) as run:
            with self.request({'field_id':'explicit-cache-id','target_grid':self.grid,
                               'observation_id':'a'*24}) as response:
                result=json.loads(response.read())
        load.assert_called_once_with('a'*24)
        run.assert_called_once_with('explicit-cache-id',target_grid=self.grid,
            satellite_observation_mask=labels,satellite_coverage_mask=coverage,
            satellite_observation_metadata=metadata)
        self.assertEqual(result['satellite_comparison']['status'],'comparison_only')

    def test_multipart_import_accepts_only_named_three_part_local_package(self):
        metadata={'schema_version':'arktika.pmc_satellite_observation.v1'}
        parts=[('labels','labels.tif',b'labels'),('coverage','coverage.tif',b'coverage'),
               ('metadata','metadata.json',json.dumps(metadata).encode())]
        returned={'observation_id':'a'*24,'observation':{'source':'test'},'status':'imported_unverified'}
        with patch.object(self.app,'register_pmc_observation',return_value=returned) as register:
            with self.multipart(parts) as response:result=json.loads(response.read())
        register.assert_called_once_with(b'labels',b'coverage',metadata)
        self.assertEqual(result,returned)
        bad=[('labels','wrong.tif',b'labels'),*parts[1:]]
        with patch.object(self.app,'register_pmc_observation',return_value=returned) as register:
            with self.assertRaises(urllib.error.HTTPError) as cm:self.multipart(bad)
            self.assertEqual(cm.exception.code,400)
            register.assert_not_called()


if __name__=='__main__':
    unittest.main()
