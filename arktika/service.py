"""Application state and background tasks. No UI toolkit or third-party dependencies."""
import calendar
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import subprocess
import threading
import time
from collections import Counter,deque
from pathlib import Path
from urllib.parse import urlsplit
from .model import PLATFORMS,CAT,LOW,UTC,iso,now,period,normalize_asset,normalize_item
from .network import Client,NetworkError,Cancelled,redact,s3_parts,s3_url
from .store import Store,import_path
from .download import DownloadQueue,download,destination


def dto(a):
    return {k:v for k,v in a.items() if k not in ('uri','self')}


def parse_filters(query):
    platform=query.get('platform','')
    if platform not in ('','ARCM1','ARCM2'):raise ValueError('Неизвестный аппарат.')
    category=query.get('category','all')
    if category not in CAT:raise ValueError('Неизвестная категория.')
    return {'platform':platform,'category':category,'channel':int(query.get('channel') or 0),
            'epsg':int(query.get('epsg') or 0),'query':str(query.get('q',''))[:300]}


class App:
    def __init__(self,state_dir,download_dir=None,token='',client=None):
        self.store=Store(state_dir)
        self.client=client or Client(token)
        self.download_dir=Path(download_dir or self.store.setting('download_dir') or Path.home()/'Downloads'/'Arktika-M').expanduser().resolve()
        self.client.mode=self.store.setting('access_mode','auto')
        self.messages=deque(maxlen=150);self.status='Готово';self.operation='';self.last_result=None
        self.lock=threading.RLock();self.cancel=threading.Event();self.task=None
        self.preview_locks={};self.queue=DownloadQueue(self.store,self.client,self.log)
        self.preview_limit=threading.BoundedSemaphore(3)
    def log(self,message):
        with self.lock:
            self.status=redact(message);self.messages.append({'time':iso(now()),'message':self.status})
    @property
    def busy(self):return bool(self.task and self.task.is_alive())
    def start(self,title,fn):
        with self.lock:
            if self.busy:raise ValueError('Другая операция ещё выполняется. Дождитесь её или нажмите «Отменить».')
            self.cancel.clear();self.operation=title;self.last_result=None
            def worker():
                self.log(title)
                try:
                    result=fn()
                    with self.lock:self.last_result={'ok':True,'result':result}
                except Cancelled:
                    self.log('Операция отменена; уже полученный каталог сохранён.')
                    with self.lock:self.last_result={'ok':False,'error':'Операция отменена'}
                except Exception as e:
                    self.log(redact(e))
                    with self.lock:self.last_result={'ok':False,'error':redact(e)}
            self.task=threading.Thread(target=worker,name='arktika-task',daemon=True);self.task.start()
    def state(self):
        with self.lock:
            status=self.status;messages=list(self.messages);result=self.last_result
        counts=Counter(j['state'] for j in self.store.jobs())
        return {'version':'2.0.0','now':iso(now()),'busy':self.busy,'operation':self.operation,'status':status,
                'result':result,'messages':messages[-30:],'revision':self.store.revision,'token':self.client.token_info(),
                'download_dir':str(self.download_dir),'data_dir':str(self.store.root),'queue_counts':dict(counts),
                'last_date':self.store.setting('last_date',iso(now())[:10]),'access_mode':self.client.mode}
    def configure(self,data):
        if 'access_mode' in data:
            if data['access_mode'] not in ('auto','public','signed'):raise ValueError('Неизвестный режим доступа.')
            self.client.mode=data['access_mode'];self.store.set_setting('access_mode',data['access_mode'])
        if 'download_dir' in data:
            p=Path(str(data['download_dir'])).expanduser()
            if not p.is_absolute():raise ValueError('Укажите абсолютный путь к папке.')
            p=p.resolve();p.mkdir(parents=True,exist_ok=True)
            import tempfile
            with tempfile.TemporaryFile(dir=str(p)) as f:f.write(b'test')
            self.download_dir=p;self.store.set_setting('download_dir',str(p))
        if 'last_date' in data:
            date=dt.date.fromisoformat(data['last_date']);self.store.set_setting('last_date',date.isoformat())
        return self.state()
    def connect(self,token):
        if self.busy:raise ValueError('Дождитесь завершения текущей операции или отмените её.')
        self.client.set_token(token)
        # Saving a new token must not enqueue or resume downloads without a click.
        def work():
            result={}
            for name,fn in (
                ('catalog',lambda:self.client.api_json('https://api.gptl.ru/stac/api/v1/catalogs/roscosmos-opendata/search?platforms=ARCM2&limit=1',cancel=self.cancel)),
                ('s3',lambda:self.client.sts(self.cancel))):
                try:
                    fn();result[name]={'ok':True}
                except NetworkError as e:result[name]={'ok':False,'error':redact(e)}
            self.log('Проверка подключения завершена. Каталог и S3 проверены отдельно; чтение файла — кнопкой «Проверить».')
            return result
        self.start('Проверка Bearer: каталог и временная сессия S3',work)
        return self.client.token_info()
    def search(self,date,scope='day',platform='',legacy=False):
        selected=dt.date.fromisoformat(date)
        if scope not in ('day','month'):raise ValueError('Поддерживается поиск за день или месяц.')
        begin,end=period(selected.year,selected.month,selected.day if scope=='day' else None)
        start,stop=iso(begin),iso(end)
        platforms=[platform] if platform else PLATFORMS
        if any(p not in PLATFORMS for p in platforms):raise ValueError('Неизвестный аппарат.')
        def work():
            results=[]
            for p in platforms:
                if self.cancel.is_set():raise Cancelled()
                try:
                    if legacy:
                        count=self.client.legacy(p,start,stop,self.store.upsert,self.log,self.cancel)
                        results.append({'platform':p,'count':count,'complete':True})
                    else:
                        self.store.scope(p,start,stop,'partial',message='Идёт поиск')
                        r=self.client.stac(p,start,stop,self.store.upsert,self.log,self.cancel)
                        self.store.scope(p,start,stop,'complete' if r['complete'] else 'partial',r['count'],r['reason'])
                        results.append(dict(r,platform=p))
                except NetworkError as e:
                    if not legacy:self.store.scope(p,start,stop,'error',message=redact(e))
                    results.append({'platform':p,'complete':False,'error':redact(e)})
                    self.log(p+': '+redact(e))
            errors=sum(bool(r.get('error')) for r in results)
            self.log(('ЦБГД' if legacy else 'STAC')+': поиск завершён'+('; ошибок: '+str(errors) if errors else '')+'.')
            return results
        self.start(('ЦБГД: ' if legacy else 'Каталог: ')+date+(' — весь месяц' if scope=='month' else ''),work)
    def calendar(self,month,filters):
        match=re.fullmatch(r'(\d{4})-(\d{2})',month)
        if not match:raise ValueError('Укажите месяц YYYY-MM.')
        year,m=map(int,match.groups());last=calendar.monthrange(year,m)[1]
        rows=self.store.assets(month,filters)
        counts=Counter(a['time'][:10] for a in rows)
        days=[];plats=[filters['platform']] if filters['platform'] else PLATFORMS
        for day in range(1,last+1):
            start=dt.datetime(year,m,day,tzinfo=UTC);stop=start+dt.timedelta(days=1)
            state=self.store.coverage(iso(start),iso(stop),plats)
            if start>now():state='future'
            elif start.date()==now().date() and state=='unknown':
                with self.store.lock:
                    rs=self.store.conn.execute('SELECT * FROM scopes WHERE start<=? AND stop>=? ORDER BY checked DESC',
                        (iso(start),iso(start))).fetchall()
                states={r['platform']:r['status'] for r in reversed(rs) if r['platform'] in plats and r['stop'][:10]==start.date().isoformat()}
                if all(states.get(p)=='complete' for p in plats):state='current'
            days.append({'date':start.date().isoformat(),'files':counts.get(start.date().isoformat(),0),'state':state})
        return {'month':month,'days':days,'total_files':len(rows)}
    def sessions(self,day,filters,offset=0,limit=24):
        dt.date.fromisoformat(day)
        assets=self.store.assets(day,filters)
        all_assets=self.store.assets(day)
        grouped={};all_grouped={}
        for a in all_assets:all_grouped.setdefault((a['platform'],a['time']),[]).append(a)
        for a in assets:grouped.setdefault((a['platform'],a['time']),[]).append(a)
        # Show raw catalogue records with no downloadable link as such, not as a successful empty search.
        if filters['category']=='raw':
            for rec in self.store.records(day,filters['platform']):
                if str(rec.get('level','')).upper() in LOW:grouped.setdefault((rec['platform'],rec['time']),[])
        sessions=[]
        for (platform,stamp),rows in sorted(grouped.items(),key=lambda kv:(kv[0][1],kv[0][0]),reverse=True):
            peers=all_grouped.get((platform,stamp),[])
            previews=[a for a in peers if a['category']=='image']
            if filters['category']=='channel' or filters['channel']:
                ch=filters['channel'] or (rows[0]['channel'] if rows else 0)
                exact=[a for a in previews if a['channel']==ch]
                previews=exact or previews
            elif filters['category']=='rgb':previews=sorted(previews,key=lambda a:'RGB' not in a['level'])
            elif filters['category']=='raw':previews=[]
            p=previews[0] if previews else None
            sessions.append({'platform':platform,'time':stamp,'count':len(rows),'size':sum(a['size'] or 0 for a in rows),
                'unknown_sizes':sum(a['size'] is None for a in rows),'categories':dict(Counter(a['category'] for a in rows)),
                'ids':[a['id'] for a in rows],'preview':p['id'] if p else '',
                'preview_caption':('Канал '+str(p['channel']) if p['channel'] else 'RGB') if p else '',
                'levels':sorted(set(a['level'] for a in rows)), 'no_uri':not bool(rows)})
        return {'sessions':sessions[offset:offset+limit],'total':len(sessions),'files':len(assets),'offset':offset,
                'ids':[a['id'] for a in assets],'size':sum(a['size'] or 0 for a in assets),
                'unknown_sizes':sum(a['size'] is None for a in assets)}
    def session(self,platform,stamp):
        if platform not in PLATFORMS:raise ValueError('Неизвестный аппарат.')
        aa=[a for a in self.store.assets(stamp,{'platform':platform}) if a['time']==stamp]
        aa.sort(key=lambda a:({'channel':0,'rgb':1,'image':2,'raw':3,'metadata':5}.get(a['category'],4),a['channel'],a['epsg'],a['filename']))
        return {'assets':[dto(a) for a in aa],'platform':platform,'time':stamp}
    def preview(self,identity):
        a=self.store.asset(identity)
        if not a or a['category']!='image':raise ValueError('Нет обзорного изображения.')
        if not a['filename'].lower().endswith(('.png','.jpg','.jpeg','.gif','.webp')):raise ValueError('Формат обзора не поддерживается.')
        with self.lock:lock=self.preview_locks.setdefault(identity,threading.Lock())
        with lock,self.preview_limit:
            folder=self.store.root/'previews';folder.mkdir(exist_ok=True)
            target=folder/(identity+Path(a['filename']).suffix.lower())
            if not target.exists():
                download(self.client,a,target,lambda *args:None,threading.Event(),max_size=32*1024*1024)
            with target.open('rb') as stream:head=stream.read(12)
            if not (head.startswith((b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff',b'GIF87a',b'GIF89a')) or (head[:4]==b'RIFF' and head[8:12]==b'WEBP')):
                raise NetworkError('Файл не распознан как обзорное изображение.')
            return target
    def check(self,identity):
        a=self.store.asset(identity)
        if not a:raise ValueError('Файл не найден.')
        result=self.client.probe(a)
        a=dict(a,access='READABLE');self.store.upsert([], [a])
        return result
    def queue_add(self,ids):
        if not isinstance(ids,list) or not ids or len(ids)>10000:raise ValueError('Выберите от 1 до 10 000 файлов.')
        ids=list(dict.fromkeys(ids));assets=[self.store.asset(i) for i in ids]
        if any(a is None for a in assets):raise ValueError('Часть файлов не найдена; обновите список.')
        self.download_dir.mkdir(parents=True,exist_ok=True)
        known=sum(a['size'] or 0 for a in assets)
        if shutil.disk_usage(str(self.download_dir)).free<known+16*1024*1024:raise ValueError('Для выбранных файлов недостаточно свободного места.')
        with self.queue.control:
            for a in assets:self.store.enqueue(a,destination(self.download_dir,a))
        return {'count':len(assets),'size':known,'path':str(self.download_dir)}
    def jobs(self):
        result=[]
        for j in self.store.jobs():
            a=self.store.asset(j['asset_id'])
            result.append(dict(j,filename=a['filename'] if a else j['asset_id'],category=a['category'] if a else 'other',
                               platform=a['platform'] if a else '',time=a['time'] if a else '',
                               speed=self.queue.runtime.get(j['id'],{}).get('speed',0),mode=self.queue.runtime.get(j['id'],{}).get('mode','')))
        priority={'running':0,'queued':1,'error':2,'paused':3,'done':4}
        return [row for _,row in sorted(enumerate(result),key=lambda pair:(priority.get(pair[1]['state'],5),-pair[0]))]
    def import_report(self,path):
        def work():
            result=import_path(self.store,path,self.log)
            self.log('Импорт завершён: {} записей, {} файлов. Полнота периода не подтверждена.'.format(result['unique_items'],result['unique_assets']))
            return result
        self.start('Импорт прежних отчётов',work)
    def storage_roots(self):
        roots=set()
        for a in self.store.assets():
            parts=s3_parts(a['uri'])
            if not parts:continue
            b,k=parts;segments=k.split('/')
            if len(segments)>=3 and 'arctic' in segments[0].lower():roots.add((b,'/'.join(segments[:2])+'/'))
        return [{'bucket':b,'prefix':p} for b,p in sorted(roots)]
    def add_uri(self,uri,size=None):
        if urlsplit(uri).scheme=='s3':self.client.validate(s3_url(*s3_parts(uri)))
        else:self.client.validate(uri)
        p=urlsplit(uri)
        if not p.path or p.path.endswith('/'):raise ValueError('Укажите файл, а папку открывайте в разделе S3.')
        ctx={'source':'direct','id':'DIRECT-'+hashlib.sha256(uri.encode()).hexdigest()[:24]}
        m=re.search(r'ETRIS\.(ARCM[12])\..*?\.(\d{4}-\d{2}-\d{2})T(\d{4})\.([^/]+)',p.path)
        ml=re.search(r'/((?:L[0-4][A-Za-z0-9_.-]*|UFD))/',p.path,re.I)
        if ml:ctx['level']=ml.group(1)
        if m:ctx.update(platform=m.group(1),time_original=m.group(2)+'T'+m.group(3)[:2]+':'+m.group(3)[2:]+':00Z',id=p.path.split('/')[-2])
        a=normalize_asset({'uri':uri,'file:size':size},ctx)
        if not a:raise ValueError('Не удалось разобрать адрес.')
        self.store.upsert([], [a]);return dto(a)
    def folders(self,path=''):
        roots=[{'name':'Домашняя папка','path':str(Path.home())},{'name':'Папка приложения','path':str(Path(__file__).resolve().parents[1])}]
        old_cache=Path.home()/'.local'/'share'/'arktika-navigator'
        if old_cache.is_dir():roots.append({'name':'Кэш терминальной версии','path':str(old_cache)})
        if os.name=='nt':
            roots.extend({'name':d+':\\','path':d+':\\'} for d in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if Path(d+':\\').exists())
        else:
            roots.append({'name':'Файловая система /','path':'/'})
            for p in ('/Volumes','/media','/mnt'):
                if Path(p).exists():roots.append({'name':p,'path':p})
        p=Path(path).expanduser().resolve() if path else Path.home()
        if not p.is_dir():raise ValueError('Папка не найдена или недоступна.')
        children=[]
        for entry in p.iterdir():
            try:
                if entry.is_dir():children.append({'name':entry.name,'path':str(entry)})
            except OSError:continue
        children.sort(key=lambda x:x['name'].casefold())
        return {'path':str(p),'parent':str(p.parent),'roots':roots,'folders':children[:2000],
                'truncated':len(children)>2000,'free':shutil.disk_usage(str(p)).free}
    def diagnostic(self):
        d=self.store.diagnostic()
        with self.client.lock:d['http_events']=list(self.client.events)
        d['platform']=sys.platform;d['python']=sys.version.split()[0]
        d['token_present']=bool(self.client.token);d['access_mode']=self.client.mode
        return d
    def close(self):
        self.cancel.set();self.queue.close()
        if self.task:self.task.join(timeout=2)
        if not self.busy and not self.queue.thread.is_alive():self.store.close()


def open_folder(path):
    path=str(Path(path).expanduser().resolve())
    if not Path(path).is_dir():raise ValueError('Папка не найдена.')
    if os.name=='nt':os.startfile(path)
    else:
        command=['open',path] if sys.platform=='darwin' else ['xdg-open',path]
        env={k:v for k,v in os.environ.items() if k not in ('GPTL_TOKEN','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN')}
        subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env)
