"""The production HTTP handlers, with explicit synthetic scientific fixtures."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from server import LocalServer
from arktika.workstation import Workstation
from arktika.processing import build_product
from arktika.geo import project_points
from test_science import fixture
from test_interpretation import profile

class AnalysisHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();root=Path(cls.tmp.name)
        scene=fixture(root);cls.app=Workstation(root/'state');cls.app.scan_local(root)
        cls.product=build_product(scene,cls.app.product_root,dict(product='micro24',preset='barents',width=256,channel=9),{'mode':'assumed'})
        cls.server=LocalServer(('127.0.0.1',0),cls.app,key='only-local-test')
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url='http://127.0.0.1:'+str(cls.server.server_address[1])
        cls.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.thread.join(3);cls.server.server_close();cls.app.close();cls.tmp.cleanup()
    def request(self,path,data=None,auth=True):
        headers={'Cookie':'arktika_session=only-local-test'} if auth else {}
        if data is not None:headers.update({'Content-Type':'application/json','X-Arktika-Request':'1'})
        req=urllib.request.Request(self.url+path,headers=headers,data=json.dumps(data).encode() if data is not None else None)
        with self.opener.open(req,timeout=5) as r:return r.status,r.headers,r.read()
    def test_new_studio_served(self):
        status,headers,body=self.request('/studio.js');self.assertEqual(status,200);self.assertIn(b'function profileChart',body)
    def test_guides_require_session(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/api/guides',auth=False)
        self.assertEqual(cm.exception.code,401)
    def test_guides_and_profiles_contract(self):
        self.assertIn('micro24',json.loads(self.request('/api/guides')[2]))
        self.assertIn('profiles',json.loads(self.request('/api/profiles')[2]))
    def test_profile_import_point_and_export(self):
        prof=json.loads(self.request('/api/profiles/import',profile())[2])
        x,y=project_points(self.app.product_grid(self.product),[(30,70)])[0]
        a=json.loads(self.request('/api/analyse',dict(product=self.product['id'],x=x,y=y,profile_id=prof['id'],altitude_m=2000,cloud_confirmed=True,opaque_confirmed=True))[2])
        self.assertEqual(a['profile_result']['layer']['status'],'supercooled')
        self.assertTrue(a['profile_result']['profile_plot']['height_m'])
        exported=json.loads(self.request('/analysis-export/'+a['id'])[2]);self.assertEqual(exported,a)
    def test_projection_has_geodesic_line(self):
        r=json.loads(self.request('/api/project-points',dict(product=self.product['id'],points=[[0,70],[60,70]]))[2])
        self.assertEqual(len(r['points']),2);self.assertGreater(len(r['line']),3)
    def test_bad_profile_is_400(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/api/profiles/import',{'source':'TEST','height':[]})
        self.assertEqual(cm.exception.code,400)
