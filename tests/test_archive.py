"""RGB/COG фикстуры синтетические: только проверка программы, не наблюдения."""
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds

from arktika.archive import read_composite, build_composite, inspect_composite
from arktika.auth import AuthClient
from arktika.model import normalize_item
from arktika.network import Response
from arktika.workstation import Workstation
from test_transport import Raw

STAMP='2024-09-20T19:00:00Z'


def rgb_fixture(path, crs=4326, alpha=False, dtype='uint8', tags=True):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    count=4 if alpha else 3
    a=np.zeros((count,64,64),dtype=dtype)
    yy,xx=np.indices((64,64));a[0]=xx*3;a[1]=yy*3;a[2]=70
    a[:3,10,10]=0  # Чёрный цвет — валидные данные, не автоматический nodata.
    if alpha:a[3]=255;a[3,:4]=0;a[3,4:8]=128
    if dtype=='uint16':a*=257
    bounds=transform_bounds(4326,crs,20,65,50,85)
    with rasterio.open(path,'w',driver='GTiff',crs=f'EPSG:{crs}',count=count,dtype=dtype,
                       height=64,width=64,transform=from_bounds(*bounds,64,64)) as ds:
        ds.write(a);ds.colorinterp=(ColorInterp.red,ColorInterp.green,ColorInterp.blue)+((ColorInterp.alpha,) if alpha else ())
        if tags:ds.update_tags(platform='ARCM1',time=STAMP,level='L2_RGB_VIS_IR',source='SYNTHETIC SOFTWARE TEST')
    return a


def archive_assets(stamp=STAMP):
    raw=dict(type='Feature',id='SYNTHETIC-ARCHIVE-'+stamp,properties={
        'platform':'ARCM1','datetime':stamp,'processing:level':'L2_RGB_VIS_IR'},assets={})
    for epsg in (4326,3857):
        raw['assets'][f'rgb.{epsg}']=dict(href=f'https://s3.gptl.ru/SYNTHETIC-TEST/{stamp[:10]}/rgb.{epsg}.cog.tif',
            type='image/tiff; application=geotiff; profile=cloud-optimized',**{'proj:epsg':epsg})
    return normalize_item(raw,'synthetic-software-test')


class RGBTransport:
    """Сетевой контракт источника подменён; очередь и загрузчик настоящие."""
    def __init__(self,payload):self.payload=payload;self.calls=0
    def open(self,url,headers=None,method='GET',body=None,timeout=45):
        self.calls+=1;headers=headers or {};data=self.payload
        begin,end=headers.get('Range','bytes=0-'+str(len(data)-1))[6:].split('-')
        begin,end=int(begin),min(int(end),len(data)-1);part=data[begin:end+1]
        return Response(Raw(206,part,{'Content-Range':f'bytes {begin}-{end}/{len(data)}',
            'Content-Length':str(len(part)),'Content-Type':'image/tiff','ETag':'"SYNTHETIC-RGB-v1"'}))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.app=Workstation(self.root/'state',download_dir=self.root/'downloads')
        self.record,self.assets=archive_assets();self.app.store.upsert([self.record],self.assets)
    def tearDown(self):self.app.close();self.temp.cleanup()
    def local(self,epsg=4326,**options):
        asset=next(a for a in self.assets if a['epsg']==epsg);path=self.root/asset['filename']
        rgb_fixture(path,epsg,**options)
        self.app.store.enqueue(asset,path);self.app.store.update_job(asset['id'],state='done',done=path.stat().st_size)
        self.app.sync_downloads();return asset,path
    def build(self):
        scene=self.app.scene(self.app.scenes()[0]['id'])
        return build_composite(scene,self.app.product_root,dict(preset='barents',width=256,channel=9))
    def test_remote_archive_not_missing_ten_channels(self):
        row=self.app.sessions('2024-09-20',{})['sessions'][0]
        self.assertTrue(row['archive_only']);self.assertEqual(len(row['imagery']),2)
        self.assertEqual(row['imagery'][0]['epsg'],4326)
    def test_preexisting_done_job_recovers_without_redownload(self):
        _,path=self.local(tags=False)
        self.assertEqual(len(self.app.scenes()[0]['composites']),1)
        self.assertEqual(self.app.scenes()[0]['channels'],[])
        self.app.close();self.app=Workstation(self.root/'state')
        self.assertEqual(self.app.scenes()[0]['time'],STAMP)
        self.assertTrue(path.exists())
    def test_composite_registered_from_catalog_not_filename(self):
        self.local(tags=False);self.assertEqual(self.app.scenes()[0]['platform'],'ARCM1')
    def test_both_projections_can_be_opened(self):
        for epsg in (4326,3857):self.local(epsg)
        scene=self.app.scene(self.app.scenes()[0]['id'])
        for entry in scene['composites'].values():
            p=build_composite(scene,self.app.product_root,dict(preset='barents',width=256,composite=entry['id']))
            self.assertGreater(p['legend']['valid_pixels'],0)
            self.assertEqual(p['legend']['source_crs'],entry['crs'])
    def test_session_has_openable_file_and_no_private_path(self):
        asset,_=self.local();r=self.app.session('ARCM1',STAMP)
        self.assertTrue(r['local_id']);self.assertEqual(r['imagery'][0]['state'],'local')
        self.assertEqual(next(a for a in r['assets'] if a['id']==asset['id'])['composite_id'],asset['id'])
        self.assertNotIn(str(self.root),json.dumps(r))
    def test_color_values_preserved(self):
        _,path=self.local(alpha=True)
        with rasterio.open(path) as ds:g=dict(crs=ds.crs,width=64,height=64,transform=ds.transform)
        out=read_composite(path,g)
        self.assertEqual(out[10,10].tolist(),[0,0,0,255])
        self.assertEqual(out[20,30].tolist(),[90,60,70,255])
        self.assertEqual(int(out[2,20,3]),0)
        self.assertEqual(int(out[6,20,3]),128)
    def test_uint16_colors_are_not_temperatures(self):
        _,path=self.local(alpha=True,dtype='uint16')
        with rasterio.open(path) as ds:g=dict(crs=ds.crs,width=64,height=64,transform=ds.transform)
        self.assertEqual(read_composite(path,g)[20,30].tolist(),[90,60,70,255])
    def test_rgb_without_alpha_keeps_black(self):
        _,path=self.local()
        with rasterio.open(path) as ds:g=dict(crs=ds.crs,width=64,height=64,transform=ds.transform)
        self.assertEqual(read_composite(path,g)[10,10].tolist(),[0,0,0,255])
    def test_dataset_mask_respected(self):
        _,path=self.local()
        with rasterio.open(path,'r+') as ds:
            mask=np.full((64,64),255,dtype='uint8');mask[:3]=0;ds.write_mask(mask)
            g=dict(crs=ds.crs,width=64,height=64,transform=ds.transform)
        self.assertEqual(int(read_composite(path,g)[0,0,3]),0)
    def test_palette(self):
        path=self.root/'palette.tif'
        with rasterio.open(path,'w',driver='GTiff',count=1,width=64,height=64,dtype='uint8',crs=4326,transform=from_bounds(20,65,50,85,64,64)) as ds:
            ds.write(np.ones((64,64),dtype='uint8'),1);ds.write_colormap(1,{0:(0,0,0,255),1:(20,40,60,255)})
            g=dict(crs=ds.crs,width=64,height=64,transform=ds.transform)
        self.assertEqual(read_composite(path,g)[10,10].tolist(),[20,40,60,255])
    def test_nodata_and_footprint_transparent(self):
        self.local();p=self.build()
        with rasterio.open(self.app.artifact(p['id'],'display.tif')) as ds:
            alpha=ds.read(4);self.assertTrue((alpha==0).any());self.assertTrue((alpha==255).any())
    def test_export_has_provenance_no_invented_values(self):
        self.local();p=self.build();self.assertTrue(p['display_only']);self.assertEqual(p['legend']['units'],'RGB')
        self.assertNotIn('values.tif',p['files']);self.assertNotIn(str(self.root),json.dumps(p))
        with zipfile.ZipFile(io.BytesIO(self.app.export(p['id']))) as z:
            meta=json.loads(z.read('product.json'));self.assertEqual(len(meta['inputs'][0]['sha256']),64)
    def test_analysis_and_route_rejected(self):
        self.local();p=self.build()
        for fn in (self.app.analyse,self.app.route):
            with self.assertRaisesRegex(ValueError,'RGB'):fn(dict(product=p['id'],x=100,y=100,points=[[30,70],[31,71]]))
    def test_deleted_source_is_not_local(self):
        _,path=self.local();path.unlink();self.assertEqual(self.app.scenes(),[])
        self.assertEqual(self.app.sessions('2024-09-20',{})['sessions'][0]['imagery'][0]['state'],'remote')
    def test_changed_source_is_reinspected(self):
        asset,path=self.local();path.write_bytes(b'not TIFF')
        self.app.sync_downloads();self.assertIn(asset['id'],self.app.registration_errors);self.assertEqual(self.app.scenes(),[])
    def test_idempotent_registration(self):
        self.local();revision=self.app.store.revision
        self.app.sync_downloads();self.app.sync_downloads();self.assertEqual(self.app.store.revision,revision)
    def test_missing_rgb_layout_is_explained(self):
        asset=self.assets[0];path=self.root/'gray.tif'
        with rasterio.open(path,'w',driver='GTiff',crs=4326,count=1,width=8,height=8,dtype='uint16',transform=from_bounds(20,65,50,85,8,8)) as ds:ds.write(np.ones((8,8),dtype='uint16'),1)
        self.app.store.enqueue(asset,path);self.app.store.update_job(asset['id'],state='done')
        r=self.app.session('ARCM1',STAMP);self.assertEqual(r['imagery'][0]['state'],'unregistered')
        item=next(a for a in r['assets'] if a['id']==asset['id'])
        self.assertTrue(item['map_error']);self.assertIsNone(item['composite_id'])
    def test_xml_disguised_as_tiff_rejected_before_gdal(self):
        path=self.root/'unsafe.tif';path.write_text('<VRTDataset/>')
        with self.assertRaisesRegex(ValueError,'XML/VRT'):inspect_composite(path,self.assets[0])
        with self.assertRaisesRegex(ValueError,'XML/VRT'):read_composite(path,{})
    def test_local_tagged_import(self):
        path=self.root/'image.tif';rgb_fixture(path)
        result=self.app.scan_local(self.root);self.assertEqual(result['files'],1)
        self.assertEqual(self.app.scenes()[0]['time'],STAMP)
    def test_cache_separates_observation_times(self):
        self.local();p=self.build();scene=self.app.scene(p['scene_id']);scene=dict(scene,id='OTHER',time='2024-09-20T19:15:00Z')
        other=build_composite(scene,self.app.product_root,dict(preset='barents',width=256,channel=9))
        self.assertNotEqual(p['id'],other['id'])
    def test_completed_queue_opens_rgb(self):
        self.local();job=next(j for j in self.app.jobs() if j['state']=='done')
        self.assertTrue(job['can_open']);self.assertIsNotNone(job['composite_id'])
    def test_queue_display_newest_first_scheduler_fifo(self):
        self.app.queue.close()
        a,b=self.assets
        self.app.store.enqueue(a,self.root/'a.tif');self.app.store.enqueue(b,self.root/'b.tif')
        self.assertEqual([j['id'] for j in self.app.store.jobs()],[a['id'],b['id']])
        self.assertEqual([j['id'] for j in self.app.jobs()],[b['id'],a['id']])
        self.app.store.update_job(a['id'],state='running')
        self.assertEqual(self.app.jobs()[0]['id'],a['id'])
    def test_real_download_pipeline_with_test_transport(self):
        path=self.root/'payload.tif';rgb_fixture(path)
        transport=RGBTransport(path.read_bytes());self.app.close()
        self.app=Workstation(self.root/'pipeline-state',download_dir=self.root/'download',client=AuthClient(transport=transport))
        self.app.store.upsert([self.record],self.assets);self.app.queue_add([self.assets[0]['id']])
        end=time.monotonic()+10
        while time.monotonic()<end and self.app.store.job(self.assets[0]['id'])['state']!='done':time.sleep(.05)
        self.assertEqual(self.app.store.job(self.assets[0]['id'])['state'],'done')
        scene=self.app.scenes()[0];self.app.prepare(dict(scene=scene['id'],product='archive_rgb',preset='barents',width=256))
        self.app.task.join(10);self.assertTrue(self.app.state()['result']['ok']);self.assertGreater(transport.calls,0)

if __name__=='__main__':unittest.main()
