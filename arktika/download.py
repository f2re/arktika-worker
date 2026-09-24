"""Validated streaming downloads. S3 errors never become .tif or .part contents."""
import hashlib
import http.client
import json
import os
import re
import shutil
import threading
import tempfile
import time
from pathlib import Path
from .model import safe_component, iso, now
from .network import NetworkError, Cancelled, redact, error_hint

CHUNK=8*1024*1024
READ_BLOCK=256*1024


def destination(root,asset):
    stamp=asset.get('time') or ''
    date=stamp[:10].replace('-','/') if len(stamp)>=10 else 'unknown_date'
    # Short, cross-platform directory; retain supplier filename (including paired png/pngw).
    session=(stamp[11:16].replace(':','') or 'unknown')+'_'+safe_component(asset.get('level') or 'product',24)+'_'+hashlib.sha256((asset.get('item_id') or asset['id']).encode()).hexdigest()[:8]
    return Path(root).expanduser().resolve()/safe_component(asset.get('platform') or 'ARCM',16)/date/session/safe_component(asset['filename'],120)


def digest(path,cancel=None):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):
            if cancel and cancel.is_set():raise Cancelled()
            h.update(block)
    return h.hexdigest()


def atomic_json(path,obj):
    """Atomic replacement with a per-writer temporary file in the same directory."""
    path=Path(path)
    if path.is_symlink():raise ValueError('Служебный JSON не должен быть символьной ссылкой.')
    # Validate before creating the temp file. RFC 8259 forbids NaN and Infinity.
    content=json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            f.write(content);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def bad_payload(data,ctype,filename):
    head=data[:256].lstrip().lower()
    if 'text/html' in ctype or head.startswith((b'<!doctype html',b'<html',b'<error')):return True
    if filename.lower().endswith(('.tif','.tiff')):
        return not data.startswith((b'II*\x00',b'MM\x00*',b'II+\x00',b'MM\x00+'))
    if filename.lower().endswith('.png'):return not data.startswith(b'\x89PNG\r\n\x1a\n')
    return False


def object_validator(metadata):
    """Choose a byte-identity validator. A weak ETag cannot protect Range resume."""
    etag = metadata.get('etag', '')
    if isinstance(etag, str) and re.fullmatch(r'"[^"\r\n]*"', etag):
        return 'etag', etag
    modified = metadata.get('last_modified', '')
    if modified:
        from email.utils import parsedate_to_datetime
        try:
            parsed = parsedate_to_datetime(modified)
            response_date = parsedate_to_datetime(metadata.get('response_date', ''))
            # RFC 9110 8.8.2.2: conservatively infer a strong HTTP-date validator.
            if parsed.tzinfo is not None and response_date.tzinfo is not None and (response_date-parsed).total_seconds() >= 60:
                return 'last_modified', modified
        except (TypeError, ValueError, OverflowError):
            pass
    return '', ''


def download(client,asset,target,progress,cancel,chunk_size=CHUNK,max_size=None):
    target=Path(target)
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.is_symlink():raise NetworkError('Путь назначения — символьная ссылка. Запись отклонена.')
    part=Path(str(target)+'.part');mp=Path(str(target)+'.part.json');done=Path(str(target)+'.download.json')
    if part.is_symlink() or mp.is_symlink() or done.is_symlink():raise NetworkError('Служебный путь — символьная ссылка.')
    if target.exists():
        try:
            info=json.loads(done.read_text(encoding='utf-8'))
            if info.get('asset_id')==asset['id'] and info.get('size')==target.stat().st_size and info.get('sha256')==digest(target,cancel):
                progress(target.stat().st_size,target.stat().st_size,info.get('access_mode',''))
                return info
        except (OSError,ValueError):pass
        raise NetworkError('Файл уже существует, но не подтверждён журналом загрузки. Он не перезаписан.')
    metadata={}
    if mp.exists():
        try:metadata=json.loads(mp.read_text(encoding='utf-8'))
        except (ValueError,OSError):raise NetworkError('Не читается .part.json. Начните заново через кнопку в очереди.')
    offset=part.stat().st_size if part.exists() else 0
    if offset and metadata.get('asset_id')!=asset['id']:raise NetworkError('Частичный файл относится к другому объекту и не изменён.')
    validator_kind,validator=object_validator(metadata)
    if offset and not validator:raise NetworkError('Без ETag/Last-Modified нельзя безопасно продолжить. Используйте «Заново».')
    expected=metadata.get('total',asset.get('size'))
    if expected is not None and offset>expected:raise NetworkError('Частичный файл длиннее ожидаемого объекта.')
    if max_size is not None and expected is not None and expected>max_size:raise NetworkError('Размер превышает ограничение этой операции.')
    first=not part.exists();mode=''
    while first or expected is None or offset<expected:
        first=False
        if cancel.is_set():raise Cancelled()
        end=offset+chunk_size-1
        if expected is not None and expected>0:end=min(end,expected-1)
        headers={'Range':'bytes={}-{}'.format(offset,end)}
        if validator_kind=='etag':
            headers['If-Match']=validator
        elif validator_kind=='last_modified':
            headers['If-Unmodified-Since']=validator
        with client.open_object(asset['uri'],headers,cancel,asset.get('region') or 'ext-dc1') as r:
            mode=r.mode;hdr=r.headers
            if r.status==412:raise NetworkError(error_hint(412,'PreconditionFailed'),412,'PreconditionFailed')
            if r.status==416 and expected==offset and (offset>0 or hdr.get('content-range')=='bytes */0'):break
            if r.status not in (200,206):raise NetworkError(error_hint(r.status,''),r.status)
            if hdr.get('content-encoding','identity').lower() not in ('','identity'):
                raise NetworkError('Сжатый HTTP-ответ не подходит для побайтового возобновления.')
            if r.status==206:
                m=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',hdr.get('content-range','').strip())
                if not m:raise NetworkError('Нет корректного Content-Range. Файл не изменён.')
                begin,finish,total=map(int,m.groups())
                if begin!=offset or finish>end or finish<begin or finish>=total:
                    raise NetworkError('Диапазон ответа не совпал с запросом. Файл не изменён.')
                response_size=finish-begin+1
            else:
                if offset:raise NetworkError('Сервер проигнорировал Range при продолжении. Разные версии не объединены; начните заново.')
                length=hdr.get('content-length')
                if length is None or not length.isdigit():raise NetworkError('Сервер не сообщил размер и не поддержал Range.')
                total=response_size=int(length)
            if expected is not None and total!=expected:
                raise NetworkError('Размер на сервере изменился: {} вместо {} байт. Обновите каталог.'.format(total,expected))
            if max_size is not None and total>max_size:raise NetworkError('Файл превышает ограничение предварительного просмотра.')
            if hdr.get('content-length') and int(hdr['content-length'])!=response_size:
                raise NetworkError('Content-Length не совпадает с диапазоном.')
            etag=hdr.get('etag','');modified=hdr.get('last-modified','')
            current_kind,current=object_validator({'etag':etag,'last_modified':modified,'response_date':hdr.get('date','')})
            if validator:
                returned=etag if validator_kind=='etag' else modified
                if returned!=validator:raise NetworkError('Валидатор объекта изменился или отсутствует. Части разных версий не объединены.')
            if r.status==206 and offset+response_size<total and not (current or validator):
                raise NetworkError('Для многодиапазонной загрузки требуется ETag или Last-Modified.')
            if shutil.disk_usage(str(target.parent)).free < total-offset+4*1024*1024:
                raise NetworkError('Недостаточно свободного места в папке назначения.')
            # Read the first bytes before creating/updating .part: reject HTML/XML disguised as TIFF.
            first_block=r.read(min(READ_BLOCK,response_size)) if response_size else b''
            if offset==0 and response_size and bad_payload(first_block,hdr.get('content-type','').lower(),asset['filename']):
                raise NetworkError('Вместо ожидаемого файла получена страница ошибки или другой формат. Данные не записаны.')
            if response_size and not first_block:raise NetworkError('Сервер вернул пустой ответ.')
            expected=total
            if not validator:validator_kind,validator=current_kind,current
            metadata.update(asset_id=asset['id'],total=total,etag=etag or metadata.get('etag',''),last_modified=modified or metadata.get('last_modified',''),response_date=hdr.get('date','') or metadata.get('response_date',''))
            atomic_json(mp,metadata)
            received=0
            try:
                with part.open('ab') as f:
                    block=first_block
                    while block:
                        if cancel.is_set():raise Cancelled()
                        if received+len(block)>response_size:raise NetworkError('Сервер передал больше байт, чем заявил.')
                        f.write(block);offset+=len(block);received+=len(block)
                        progress(offset,total,mode)
                        if received>=response_size:break
                        block=r.read(min(READ_BLOCK,response_size-received))
                    f.flush();os.fsync(f.fileno())
            except (OSError,http.client.HTTPException) as e:
                raise NetworkError('Связь прервана. Полученные байты сохранены; нажмите «Продолжить». '+redact(e)) from e
            if received!=response_size:raise NetworkError('Ответ оборвался. Полученные байты сохранены для продолжения.')
        if r.status==200 or total==0:break
    if cancel.is_set():raise Cancelled()
    if not part.exists():part.touch()
    if part.stat().st_size!=expected:raise NetworkError('Размер загруженного файла не совпал с ожидаемым.')
    sha=digest(part,cancel)
    # Atomic rename only after a complete, validated response.
    os.replace(str(part),str(target))
    info={'asset_id':asset['id'],'size':expected,'sha256':sha,'time':iso(now()),'access_mode':mode,
          'etag':metadata.get('etag',''),'note':'SHA-256 вычислен локально, не является сверкой с суммой поставщика.'}
    atomic_json(done,info)
    try:mp.unlink()
    except FileNotFoundError:pass
    return info


class DownloadQueue:
    def __init__(self,store,client,log):
        self.store=store;self.client=client;self.log=log
        self.stopped=threading.Event();self.cancel=threading.Event();self.control=threading.RLock()
        self.active=None;self.runtime={}
        self.thread=threading.Thread(target=self.run,name='arktika-download',daemon=True);self.thread.start()
    def pause(self,identity=None):
        with self.control:
            if not identity or identity==self.active:self.cancel.set()
            for j in self.store.jobs():
                if (not identity or j['id']==identity) and j['state']=='queued':self.store.update_job(j['id'],state='paused')
    def resume(self,identity=None):
        with self.control:
            for j in self.store.jobs():
                if (not identity or j['id']==identity) and j['state'] in ('paused','error'):
                    self.store.update_job(j['id'],state='queued',error='')
    def restart(self,identity):
        with self.control:
            j=self.store.job(identity)
            if not j or j['state'] in ('running','queued'):raise ValueError('Сначала приостановите эту загрузку.')
            # Only our own partial data, never supplier files or an existing final file.
            target=Path(j['path'])
            for suffix in ('.part','.part.json'):
                p=Path(str(target)+suffix)
                if p.exists() and not p.is_symlink():p.unlink()
            self.store.update_job(identity,state='queued',done=0,error='')
    def remove(self,identity):
        with self.control:
            j=self.store.job(identity)
            if j and j['state']=='running':raise ValueError('Сначала приостановите загрузку.')
            with self.store.lock,self.store.conn:
                self.store.conn.execute('DELETE FROM jobs WHERE id=?',(identity,));self.store.revision+=1
    def run(self):
        while not self.stopped.wait(.15):
            with self.control:
                job=next((j for j in self.store.jobs() if j['state']=='queued'),None)
                if not job:continue
                self.active=job['id'];self.cancel.clear()
                self.store.update_job(job['id'],state='running',error='')
            last=[0.,job['done']];started=time.monotonic();initial=job['done']
            def progress(done,total,mode):
                elapsed=max(time.monotonic()-started,.001)
                with self.control:self.runtime[job['id']]={'speed':max(0,done-initial)/elapsed,'mode':mode}
                if time.monotonic()-last[0]>.25 or done==total:
                    self.store.update_job(job['id'],done=done,total=total);last[:]=[time.monotonic(),done]
            try:
                asset=self.store.asset(job['asset_id'])
                if not asset:raise ValueError('Файл отсутствует в локальном индексе.')
                info=download(self.client,asset,job['path'],progress,self.cancel)
                self.store.update_job(job['id'],state='done',done=info['size'],total=info['size'],sha256=info['sha256'])
                self.log('Загружен '+asset['filename'])
            except Cancelled:
                self.store.update_job(job['id'],state='paused')
            except Exception as e:
                self.store.update_job(job['id'],state='error',error=redact(e))
                self.log('Загрузка: '+redact(e))
                if isinstance(e,NetworkError) and (e.status==401 or e.code in ('ExpiredToken','InvalidToken','InvalidAccessKeyId','MissingBearer')):self.pause()
            finally:
                part=Path(str(job['path'])+'.part')
                if part.exists():self.store.update_job(job['id'],done=part.stat().st_size)
                with self.control:
                    self.active=None
                    if job['id'] in self.runtime:self.runtime[job['id']]['speed']=0
    def close(self):
        self.stopped.set();self.pause();self.thread.join(timeout=3)
