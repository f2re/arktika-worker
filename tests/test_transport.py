"""Release smoke and transport regression checks; fixtures are not satellite data."""
import ast
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from server import LocalServer, ROOT
from arktika.model import normalize_asset, normalize_item
from arktika.network import Client, NetworkError, Response
from arktika.download import download
from arktika.service import App

# Deliberate byte fixture: not a valid scientific dataset or decoded image.
DATA = b'II*\x00' + bytes(range(256)) * 40
URI = 'https://s3.gptl.ru/package-test-only/fixture_ch04.tif'

def asset():
    return normalize_asset({'href': URI, 'item_id': 'package-fixture',
        'platform': 'ARCM2', 'level': 'L2IR', 'time': '2026-09-21T06:00:00Z',
        'type': 'image/tiff; application=geotiff', 'file:size': len(DATA),
        'proj:epsg': 4326})

class Raw(io.BytesIO):
    def __init__(self, code, body, headers=None):
        super().__init__(body)
        self.code = code
        self.headers = headers or {}

class FakeTransport:
    def __init__(self, signed_fails=False, html=False):
        self.calls = []
        self.signed_fails = signed_fails
        self.html = html
    def open(self, url, headers=None, method='GET', body=None, timeout=45):
        headers = headers or {}
        self.calls.append((url, dict(headers)))
        if self.signed_fails and any(k.lower()=='authorization' for k in headers):
            return Response(Raw(403, b'<Error><Code>AccessDenied</Code></Error>'))
        if self.html:
            bad = b'<html>error</html>'
            return Response(Raw(200, bad, {'Content-Length': str(len(bad)), 'Content-Type':'text/html'}))
        begin, end = headers.get('Range', 'bytes=0-'+str(len(DATA)-1))[6:].split('-')
        begin, end = int(begin), min(int(end), len(DATA)-1)
        payload = DATA[begin:end+1]
        return Response(Raw(206, payload, {'Content-Range': 'bytes {}-{}/{}'.format(begin, end, len(DATA)),
            'Content-Length': str(len(payload)), 'Content-Type': 'image/tiff', 'ETag':'"package-fixture-v1"'}))

class PackageChecks(unittest.TestCase):
    def test_runtime_files_present(self):
        for name in ('server.py','start.sh','start.cmd','start.command','static/index.html',
                     'static/app.js','static/style.css','static/icon.svg','README.md'):
            self.assertTrue((ROOT/name).is_file(), name)
    def test_python_syntax_310(self):
        for path in [ROOT/'server.py', ROOT/'check.py'] + list((ROOT/'arktika').glob('*.py')):
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path), feature_version=(3,10))
    def test_channel_is_not_raw(self):
        self.assertEqual(asset()['category'], 'channel')
        self.assertEqual(asset()['channel'], 4)
    def test_asset_id_ignores_temporary_query(self):
        a = asset()
        other = normalize_asset(dict(a, uri=URI+'?X-Amz-Signature=test-only'))
        self.assertEqual(a['id'],other['id'])
    def test_bearer_prefix_and_hidden_state(self):
        client = Client('Authorization: Bearer PACKAGE-TEST-NOT-A-CREDENTIAL', transport=FakeTransport())
        self.assertEqual(client.token, 'PACKAGE-TEST-NOT-A-CREDENTIAL')
        self.assertNotIn(client.token, json.dumps(client.token_info()))
    def test_public_object_does_not_receive_bearer(self):
        http = FakeTransport(); client = Client('PACKAGE-TEST', transport=http)
        with client.open_object(URI) as r:
            self.assertEqual(r.mode, 'public')
        self.assertFalse(any(k.lower()=='authorization' for k in http.calls[0][1]))
    def test_signed_403_falls_back_to_public(self):
        http = FakeTransport(signed_fails=True); client = Client('PACKAGE-TEST', transport=http)
        client.credentials = {'AccessKeyId':'fixture-access','SecretAccessKey':'fixture-secret','SessionToken':'fixture-session'}
        client.cred_expiry = time.time()+3600
        client.preferred[('s3.gptl.ru','package-test-only')] = 'sts'
        with client.open_object(URI) as r:
            self.assertEqual(r.mode, 'public')
            self.assertEqual(r.read(),DATA)
        self.assertEqual(len(http.calls),2)
    def test_validated_download_and_reuse(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)/'sample.tif'; c = Client(transport=FakeTransport())
            info = download(c, asset(), target, lambda *x:None, threading.Event(), chunk_size=2048)
            self.assertEqual(target.read_bytes(), DATA)
            self.assertEqual(info['sha256'],hashlib.sha256(DATA).hexdigest())
            self.assertFalse(Path(str(target)+'.part').exists())
            self.assertEqual(download(c, asset(), target, lambda *x:None, threading.Event())['sha256'], info['sha256'])
    def test_html_not_saved_as_tiff(self):
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)/'bad.tif'; a=dict(asset(),size=None)
            with self.assertRaises(NetworkError):
                download(Client(transport=FakeTransport(html=True)),a,target,lambda *x:None,threading.Event())
            self.assertFalse(target.exists());self.assertFalse(Path(str(target)+'.part').exists())
    def test_untrusted_external_host_rejected(self):
        with self.assertRaises(NetworkError):
            Client(transport=FakeTransport()).open_object('https://example.invalid/object.tif')

class ServerChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.app=App(Path(cls.temp.name)/'data', Path(cls.temp.name)/'downloads', client=Client(transport=FakeTransport()))
        cls.srv=LocalServer(('127.0.0.1',0),cls.app,key='package-local-test-key')
        cls.thread=threading.Thread(target=cls.srv.serve_forever,daemon=True);cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.srv.server_address[1])
        cls.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown();cls.thread.join(3);cls.srv.server_close();cls.app.close();cls.temp.cleanup()
    def get(self,path,authenticated=False):
        headers={'Cookie':'arktika_session=package-local-test-key'} if authenticated else {}
        return self.opener.open(urllib.request.Request(self.base+path,headers=headers),timeout=5)
    def test_01_health(self):
        with self.get('/health') as r:self.assertEqual(json.load(r)['app'],'arktika-web')
    def test_02_all_static_assets(self):
        for path in ('/','/app.js','/style.css','/icon.svg'):
            with self.get(path) as r:
                self.assertEqual(r.status,200);self.assertTrue(r.read())
    def test_03_private_state_requires_local_session(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:self.get('/api/state')
        self.assertEqual(cm.exception.code,401)
    def test_04_bootstrap(self):
        req=urllib.request.Request(self.base+'/api/bootstrap',data=json.dumps({'key':'package-local-test-key'}).encode(),
            headers={'Content-Type':'application/json','X-Arktika-Request':'1'})
        with self.opener.open(req,timeout=5) as r:
            self.assertIn('HttpOnly',r.headers['Set-Cookie']);self.assertTrue(json.load(r)['ok'])
    def test_05_calendar_leap_year(self):
        with self.get('/api/calendar?month=2024-02',True) as r:self.assertEqual(len(json.load(r)['days']),29)
    def test_06_empty_queue_and_catalog(self):
        with self.get('/api/queue',True) as r:self.assertEqual(json.load(r)['jobs'],[])
        with self.get('/api/catalog?day=2026-09-21',True) as r:self.assertEqual(json.load(r)['files'],0)
    def test_07_cross_origin_blocked(self):
        req=urllib.request.Request(self.base+'/api/state',headers={'Cookie':'arktika_session=package-local-test-key','Origin':'https://example.invalid'})
        with self.assertRaises(urllib.error.HTTPError) as cm:self.opener.open(req,timeout=5)
        self.assertEqual(cm.exception.code,403)
    def test_08_post_requires_local_header(self):
        req=urllib.request.Request(self.base+'/api/settings',data=b'{}',headers={'Cookie':'arktika_session=package-local-test-key','Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as cm:self.opener.open(req,timeout=5)
        self.assertEqual(cm.exception.code,403)

if __name__=='__main__':unittest.main(verbosity=2)
