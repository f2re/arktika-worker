#!/usr/bin/env python3
"""Local-only Arktika-M web navigator. Run: python server.py [--no-browser]."""
import sys
if sys.version_info < (3,10):
    raise SystemExit('Требуется Python 3.10 или новее.')
import argparse
import hmac
import json
import mimetypes
import os
import secrets
import socket
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,unquote
from arktika.service import App,parse_filters,open_folder
from arktika.network import NetworkError,redact
from arktika.instance import InstanceLock
from arktika.workstation import Workstation
from arktika.products import registry
from arktika.interpretation import GUIDES
from arktika.profiles import integrate,cloud_top

ROOT=Path(__file__).resolve().parent

class LocalServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,address,app,key=None):
        self.app=app;self.key=key or secrets.token_urlsafe(32)
        self.slots=threading.BoundedSemaphore(24)
        super().__init__(address,Handler)
    def process_request(self,request,client_address):
        if not self.slots.acquire(blocking=False):
            request.close();return
        try:super().process_request(request,client_address)
        except Exception:self.slots.release();raise
    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.slots.release()

class Handler(BaseHTTPRequestHandler):
    server_version='ArktikaWorker/0.2'
    protocol_version='HTTP/1.1'
    def setup(self):
        super().setup();self.connection.settimeout(120)
    def log_message(self,format,*args):
        pass  # URLs, tokens, signed queries and request bodies are not logged.
    def security(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
    def send(self,status,body,ctype='application/json; charset=utf-8',extra=None):
        if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False,allow_nan=False).encode('utf-8')
        self.send_response(status);self.security()
        self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store' if not ctype.startswith('image/') else 'private, max-age=600')
        for k,v in (extra or {}).items():self.send_header(k,v)
        self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass
    def gate(self,authenticated=True):
        port=self.server.server_address[1]
        allowed={'127.0.0.1:'+str(port),'localhost:'+str(port)}
        host=self.headers.get('Host','').lower()
        if host not in allowed:
            self.send(403,{'error':'Недопустимый Host. Сервер доступен только локально.'});return False
        origin=self.headers.get('Origin')
        if origin and origin not in {'http://'+x for x in allowed}:
            self.send(403,{'error':'Межсайтовый запрос отклонён.'});return False
        if self.headers.get('Sec-Fetch-Site')=='cross-site':
            self.send(403,{'error':'Межсайтовый запрос отклонён.'});return False
        if authenticated:
            cookie=SimpleCookie()
            try:cookie.load(self.headers.get('Cookie',''))
            except Exception:pass
            value=cookie.get('arktika_session')
            if not value or not hmac.compare_digest(value.value,self.server.key):
                self.send(401,{'error':'Откройте адрес с ключом запуска из окна сервера.'});return False
        return True
    def do_GET(self):
        path=urlsplit(self.path).path
        if path=='/oauth/callback':
            port=self.server.server_address[1]
            if self.headers.get('Host','')!='127.0.0.1:'+str(port):
                self.send(403,{'error':'Недопустимый Host.'});return
            try:
                q={k:v[-1] for k,v in parse_qs(urlsplit(self.path).query).items()}
                self.server.app.client.callback(q.get('code',''),q.get('state',''))
                self.send(303,b'',extra={'Location':'/'})
            except Exception as e:self.error(e)
            return
        if path in ('/','/app.js','/style.css','/icon.svg','/favicon.ico','/studio.js','/catalog.js','/catalog.css','/product_flow.js'):
            if not self.gate(False):return
            names={'/':'index.html','/app.js':'app.js','/style.css':'style.css','/icon.svg':'icon.svg','/favicon.ico':'icon.svg','/studio.js':'studio.js','/product_flow.js':'product_flow.js','/catalog.js':'catalog.js','/catalog.css':'catalog.css'}
            p=ROOT/'static'/names[path]
            ctype={'index.html':'text/html; charset=utf-8','app.js':'application/javascript; charset=utf-8','studio.js':'application/javascript; charset=utf-8','product_flow.js':'application/javascript; charset=utf-8','catalog.js':'application/javascript; charset=utf-8','catalog.css':'text/css; charset=utf-8','style.css':'text/css; charset=utf-8','icon.svg':'image/svg+xml'}[p.name]
            self.send(200,p.read_bytes(),ctype);return
        if path=='/health':
            if self.gate(False):self.send(200,{'app':'arktika-web','version':'0.2.2'})
            return
        if not self.gate():return
        try:
            q={k:v[-1] for k,v in parse_qs(urlsplit(self.path).query).items()}
            app=self.server.app
            if path=='/api/registry':result=registry()
            elif path=='/api/guides':result=GUIDES
            elif path=='/api/profiles':result={'profiles':app.profiles()}
            elif path=='/api/scenes':result={'scenes':app.scenes(q.get('date',''),q.get('platform',''))}
            elif path=='/api/products':result={'products':app.products()}
            elif path=='/api/map':result=app.context(q.get('preset','arctic'),int(q.get('width',1000)),q.get('product'))
            elif path=='/api/motion-result':
                p=app.store.root/'motion.json'
                result=json.loads(p.read_text(encoding='utf-8')) if p.exists() else {'vectors':[],'candidates':[]}
            elif path.startswith('/artifact/'):
                bits=path.split('/')
                if len(bits)!=4:raise ValueError('Неверный путь продукта.')
                identity,name=bits[2:]
                if name=='export.zip':
                    self.send(200,app.export(identity),'application/zip',{'Content-Disposition':'attachment; filename="arktika-product.zip"'});return
                p=app.artifact(identity,name)
                ctype=mimetypes.guess_type(str(p))[0] or 'application/octet-stream'
                self.send(200,p.read_bytes(),ctype,{'Content-Disposition':'attachment; filename="'+name+'"'} if name.endswith('.tif') else None);return
            elif path.startswith('/analysis-export/'):
                self.send(200,app.analysis_export(path.split('/')[-1]),extra={'Content-Disposition':'attachment; filename=analysis.json'});return
            elif path.startswith('/route-export/'):
                bits=path.split('/')
                if len(bits)!=4:raise ValueError('Неверный путь маршрута.')
                identity,kind=bits[2:];content=app.route_export(identity,kind)
                self.send(200,content,'text/csv; charset=utf-8' if kind=='csv' else 'application/json',{'Content-Disposition':'attachment; filename="route.'+kind+'"'});return
            elif path.startswith('/docs/'):
                name=path[len('/docs/'):]
                if not name or '/' in name or '\\' in name or not (ROOT/'docs'/name).is_file():raise ValueError('Документ не найден.')
                f=ROOT/'docs'/name
                self.send(200,f.read_bytes(),mimetypes.guess_type(str(f))[0] or 'text/plain; charset=utf-8');return
            elif path=='/api/state':result=app.state()
            elif path=='/api/calendar':result=app.calendar(q['month'],parse_filters(q))
            elif path=='/api/catalog':result=app.sessions(q['day'],parse_filters(q),max(0,int(q.get('offset',0))),min(60,max(1,int(q.get('limit',24)))))
            elif path=='/api/session':result=app.session(q['platform'],q['time'])
            elif path=='/api/queue':result={'jobs':app.jobs()}
            elif path=='/api/storage/roots':result={'roots':app.storage_roots()}
            elif path=='/api/diagnostic':
                self.send(200,app.diagnostic(),extra={'Content-Disposition':'attachment; filename="arktika_diagnostic.json"'});return
            elif path.startswith('/preview/'):
                identity=path[len('/preview/'):]
                if not identity.isalnum() or len(identity)>64:raise ValueError('Недопустимый идентификатор.')
                p=app.preview(identity)
                ctype=mimetypes.guess_type(str(p))[0] or 'application/octet-stream'
                self.send(200,p.read_bytes(),ctype);return
            else:self.send(404,{'error':'Маршрут не найден.'});return
            self.send(200,result)
        except Exception as e:self.error(e)
    def do_POST(self):
        path=urlsplit(self.path).path
        if not self.gate(path!='/api/bootstrap'):return
        if self.headers.get('X-Arktika-Request')!='1':
            self.send(403,{'error':'Нет заголовка защиты локального запроса.'});return
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':
            self.send(415,{'error':'Ожидается application/json.'});return
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n<0 or n>4*1024*1024:
                self.close_connection=True;self.send(413,{'error':'Слишком большой запрос.'});return
            data=json.loads(self.rfile.read(n) or b'{}')
            if not isinstance(data,dict):raise ValueError('Ожидается JSON-объект.')
            if path=='/api/bootstrap':
                if not hmac.compare_digest(str(data.get('key','')),self.server.key):
                    self.send(401,{'error':'Неверный ключ запуска. Откройте ссылку из окна сервера.'});return
                self.send(200,{'ok':True},extra={'Set-Cookie':'arktika_session='+self.server.key+'; HttpOnly; SameSite=Strict; Path=/'});return
            app=self.server.app;result={'ok':True}
            if path=='/api/product-plan':result=app.product_plan(data)
            elif path=='/api/scale/inventory':result=app.scale_inventory(data)
            elif path=='/api/scale/preview':result=app.scale_proposal(data)
            elif path=='/api/scale/save':result=app.save_scale(data)
            elif path=='/api/scale/image':result=app.preview_scale_image(data)
            elif path=='/api/calibration':result=app.set_calibration(data)
            elif path=='/api/local/import':app.start('Импорт локальных GeoTIFF',lambda:app.scan_local(data['path']))
            elif path=='/api/process':app.prepare(data)
            elif path=='/api/coordinates':result=app.coordinates(data)
            elif path=='/api/project-points':
                from arktika.geo import project_points, densify_route
                points=data.get('points',[])
                if not isinstance(points,list) or len(points)>500:raise ValueError('Не более 500 точек.')
                import math
                for point in points:
                    if not isinstance(point,(list,tuple)) or len(point)!=2 or not all(math.isfinite(float(v)) for v in point) or not -180<=float(point[0])<=180 or not -90<=float(point[1])<=90:raise ValueError('Неверные координаты.')
                g=app.product_grid(app.product(data['product']))
                samples=densify_route(points,step_km=100) if len(points)>1 else []
                result={'points':project_points(g,points),'line':project_points(g,[(s['lon'],s['lat']) for s in samples]) if samples else project_points(g,points)}
            elif path=='/api/pixel':result=app.pixel(data)
            elif path=='/api/analyse':result=app.analyse(data)
            elif path=='/api/profiles/import':result=app.import_profile(data)
            elif path=='/api/route':result=app.route(data)
            elif path=='/api/motion':app.motion(data)
            elif path=='/api/profile':result=cloud_top(data) if data.get('mode')=='cloud_top' else integrate(data)
            elif path=='/api/oauth/configure':
                app.client.configure_oauth(data);app.store.set_setting('oauth',app.client.oauth)
            elif path=='/api/oauth/start':
                expected='http://127.0.0.1:'+str(self.server.server_address[1])+'/oauth/callback'
                if app.client.oauth.get('redirect_uri')!=expected:raise ValueError('redirect_uri должен совпадать с '+expected)
                result={'url':app.client.begin()}
            elif path=='/api/oauth/refresh':result=app.client.refresh(force=True)
            elif path=='/api/credentials':
                app.client.replace(data.get('token',''),data.get('refresh_token',''));result=app.client.token_info()
            elif path=='/api/token':
                if data.get('clear'):
                    if hasattr(app.client,'replace'):app.client.replace('')
                    else:app.client.set_token('')
                elif data.get('check',True):result=app.connect(data.get('token',''))
                else:app.client.set_token(data.get('token',''));result=app.client.token_info()
            elif path=='/api/settings':result=app.configure(data)
            elif path=='/api/search':app.search(data['date'],data.get('scope','day'),data.get('platform',''),bool(data.get('legacy')))
            elif path=='/api/cancel':app.cancel.set()
            elif path=='/api/import':app.import_report(data['path'])
            elif path=='/api/check':result=app.check(data['id'])
            elif path=='/api/queue':result=app.queue_add(data['ids'])
            elif path=='/api/queue/action':
                action=data['action'];identity=data.get('id')
                if action=='pause':app.queue.pause(identity)
                elif action=='resume':app.queue.resume(identity)
                elif action=='restart' and identity:app.queue.restart(identity)
                elif action=='remove' and identity:app.queue.remove(identity)
                else:raise ValueError('Недопустимое действие.')
            elif path=='/api/folders':result=app.folders(data.get('path',''))
            elif path=='/api/folders/create':
                p=Path(data['parent']).expanduser().resolve();name=str(data['name']).strip()
                if not name or '/' in name or '\\' in name or name in ('.','..') or any(ord(c)<32 for c in name):raise ValueError('Недопустимое имя папки.')
                (p/name).mkdir(exist_ok=False);result=app.folders(str(p/name))
            elif path=='/api/open-folder':
                if data.get('job'):
                    j=app.store.job(data['job'])
                    if not j:raise ValueError('Задание не найдено.')
                    open_folder(str(Path(j['path']).parent))
                else:open_folder(str(app.download_dir))
            elif path=='/api/storage':result=app.client.list_s3(data['bucket'],data.get('prefix',''),cursor=data.get('cursor',''))
            elif path=='/api/add-uri':result=app.add_uri(data['uri'],data.get('size'))
            elif path=='/api/shutdown':
                app.queue.pause();app.cancel.set()
                threading.Thread(target=self.server.shutdown,daemon=True).start()
            else:self.send(404,{'error':'Маршрут не найден.'});return
            self.send(200,result)
        except Exception as e:self.error(e)
    def error(self,e):
        if isinstance(e,(ValueError,KeyError,TypeError)):self.send(400,{'error':redact(e)})
        elif isinstance(e,NetworkError):self.send(502,{'error':redact(e),'upstream_status':e.status,'code':e.code,'attempts':e.attempts})
        elif isinstance(e,PermissionError):self.send(403,{'error':'Нет прав доступа к выбранной папке.'})
        elif isinstance(e,OSError):self.send(500,{'error':'Ошибка файловой системы: '+redact(e)})
        else:
            self.server.app.log('Внутренняя ошибка: '+redact(e))
            self.send(500,{'error':'Внутренняя ошибка: '+redact(e)})


def main(argv=None):
    parser=argparse.ArgumentParser(description='Арктика-М: локальный веб-каталог и загрузчик')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--data-dir',default=str(ROOT/'data'))
    parser.add_argument('--download-dir')
    parser.add_argument('--import',dest='import_path')
    parser.add_argument('--import-local')
    parser.add_argument('--config',default=str(ROOT/'config'/'app.json'))
    parser.add_argument('--token-file')
    args=parser.parse_args(argv)
    os.umask(0o077)
    lock=InstanceLock(args.data_dir)
    if not lock.acquire():
        url=lock.read_url()
        print('Веб-навигатор уже работает с этой базой.')
        if url:
            print(url)
            if not args.no_browser:webbrowser.open(url)
        return
    cfg_path=Path(args.config)
    cfg=json.loads(cfg_path.read_text(encoding='utf-8')) if cfg_path.exists() else {}
    token=Path(args.token_file).read_text(encoding='utf-8').strip() if args.token_file else os.environ.pop('GPTL_TOKEN','')
    app=Workstation(args.data_dir,args.download_dir or cfg.get('download_dir'),token,config=cfg)
    app.client.oauth.update(app.store.setting('oauth',{}))
    server=None
    for port in range(args.port,args.port+1):
        try:server=LocalServer(('127.0.0.1',port),app);break
        except OSError:continue
    if server is None:
        app.close();lock.close();raise SystemExit('Не удалось открыть локальный порт. Укажите --port 9000.')
    url='http://127.0.0.1:{}/#{}'.format(server.server_address[1],server.key)
    lock.publish(url)
    print('\nАрктика-М — рабочее место метеоролога 0.2.2\n')
    print('Откройте в браузере:\n'+url+'\n')
    print('Сервер работает только на этом компьютере. Не закрывайте это окно во время загрузки.')
    print('Остановка: Ctrl+C или кнопка «Завершить работу» в интерфейсе.\n',flush=True)
    if args.import_path or args.import_local:
        def initial_import():
            if args.import_path:
                from arktika.store import import_path
                import_path(app.store,args.import_path,app.log)
            if args.import_local:app.scan_local(args.import_local)
        app.start('Первичный импорт',initial_import)
    if not args.no_browser:threading.Timer(.6,lambda:webbrowser.open(url)).start()
    try:server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:print('\nОстановка, очередь сохраняется…',flush=True)
    finally:server.server_close();app.close();lock.close()

if __name__=='__main__':main()
