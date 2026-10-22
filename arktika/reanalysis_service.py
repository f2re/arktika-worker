"""API полей: задачи, проверяемый кэш, отображение и сохранённый стек слоёв."""
from __future__ import annotations
import copy
import hashlib
import io
import json
import math
from pathlib import Path
import re
import tempfile
import threading
import uuid
import zipfile
from .download import atomic_json, digest
from .era5_runtime import ensure_runtime, runtime_info, run_process, runtime_env
from .geo import grid
from .network import Cancelled
from .reanalysis.catalog import catalogue, request_plan, VERSION

ROOT=Path(__file__).resolve().parents[1]
ID=re.compile(r'^[0-9a-f]{24}$')
TASK_ID=re.compile(r'^[0-9a-f]{32}$')


class ReanalysisMixin:
    def _fields_init(self):
        self._sources_init()
        with self.lock:
            if hasattr(self,'fields_root'): return
            self.fields_root=self.store.root/'reanalysis';self.fields_root.mkdir(exist_ok=True)
            self.field_cache=self.fields_root/'fields';self.field_cache.mkdir(exist_ok=True)
            self.field_renders=self.fields_root/'renders';self.field_renders.mkdir(exist_ok=True)
            self._field_render_lock=threading.Lock()
            try: self._field_job=json.loads((self.fields_root/'job.json').read_text(encoding='utf-8'))
            except (ValueError,OSError): self._field_job={'status':'idle'}
            if self._field_job.get('status')=='running':
                self._field_job.update(status='interrupted',message='Операция прервана остановкой сервера. Готовые поля сохранены; задачу можно возобновить.')
                self._field_job['resume_available']=bool(self._field_job.get('pending',{}).get('kind')=='remote')
            # Older job records may have contained request payloads. Keep only the
            # allowlisted public task summary before exposing or rewriting them.
            self._field_job=self._sanitize_field_job(self._field_job)
            atomic_json(self.fields_root/'job.json',self._field_job)

    def field(self,identity):
        self._fields_init()
        if not isinstance(identity,str) or not ID.fullmatch(identity): raise ValueError('Неверный идентификатор поля.')
        try: meta=json.loads((self.field_cache/identity/'metadata.json').read_text(encoding='utf-8'))
        except (OSError,ValueError): raise ValueError('Поле не найдено в локальном кэше.') from None
        if meta.get('id')!=identity: raise ValueError('Идентификатор кэшированного поля не совпадает с каталогом.')
        if digest(self.field_cache/identity/'field.nc')!=meta['sha256']: raise ValueError('Контрольная сумма поля изменилась. Повторите получение данных.')
        return meta

    def fields_state(self):
        self._fields_init();fields=[]
        for p in self.field_cache.glob('*/metadata.json'):
            if not ID.fullmatch(p.parent.name): continue
            try:
                obj=json.loads(p.read_text(encoding='utf-8'))
                if not (p.parent/'field.nc').is_file(): continue
                fields.append({k:v for k,v in obj.items() if k not in ('provenance','request')})
            except (OSError,ValueError): continue
        fields.sort(key=lambda f:f.get('created_at',''),reverse=True)
        with self.lock: job=copy.deepcopy(self._field_job)
        return {'fields':fields,'stack':self.store.setting('reanalysis_stack',[]),'job':job,
                'catalogue':catalogue(),'busy':self.busy,
                'legacy_cache_count':sum(1 for p in self.field_cache.glob('*/field.json')
                    if ID.fullmatch(p.parent.name) and not (p.parent/'metadata.json').exists())}

    @staticmethod
    def _safe_field_spec(plan):
        """Return the immutable, credential/path/URI-free inputs needed to rebuild a plan."""
        if not isinstance(plan,dict): return None
        keys=('source','variable','time','level','area')
        if any(k not in plan for k in keys): return None
        return {k:copy.deepcopy(plan[k]) for k in keys}

    @classmethod
    def _sanitize_field_job(cls,job):
        if not isinstance(job,dict): return {'status':'idle'}
        result={k:copy.deepcopy(job[k]) for k in ('id','status','message','field_id','created_at','updated_at') if k in job}
        identity=job.get('id')
        if isinstance(identity,str) and TASK_ID.fullmatch(identity): result['id']=identity
        pending=job.get('pending')
        if isinstance(pending,dict) and pending.get('kind')=='remote':
            spec=cls._safe_field_spec(pending.get('spec'))
            if spec:
                result['pending']={'kind':'remote','spec':spec}
                result['resume_available']=bool(job.get('resume_available',job.get('status') in ('needs_action','interrupted')))
        action=job.get('needs_action')
        if (not isinstance(action,dict) and result.get('status')=='interrupted'
                and result.get('pending',{}).get('kind')=='remote'):
            source=result['pending']['spec']['source']
            action={'code':'credentials_required','provider':'cds' if source in ('era5','carra2') else 'earthdata','source':source}
        if result.get('status') in ('needs_action','interrupted') and isinstance(action,dict):
            if action.get('code')=='credentials_required' and action.get('provider') in ('cds','earthdata') and action.get('source') in ('era5','carra2','merra2'):
                result['needs_action']={k:action[k] for k in ('code','provider','source')}
        if 'status' not in result: result['status']='idle'
        return result

    def _field_runtime(self,download=False):
        info=runtime_info(self.store.root)
        needed={'xarray','scipy','contourpy'}
        if download: needed|={'cdsapi','pydap','netCDF4','requests'}
        if needed.intersection(info['missing']):
            if download: return ensure_runtime(self.store.root,self.cancel,self.log)
            raise ValueError('Установите библиотеки обработки полей: повторите install.sh / install.cmd.')
        return info

    def _field_process(self,payload,download=False,cancel=None,progress=None):
        info=self._field_runtime(download)
        with tempfile.TemporaryDirectory(prefix='work-',dir=self.fields_root) as temp:
            work=Path(temp);payload=dict(payload,work=str(work));last=''
            def tick():
                nonlocal last
                try: message=json.loads((work/'progress.json').read_text(encoding='utf-8'))['message']
                except (OSError,ValueError,KeyError): return
                if message!=last:
                    last=message
                    if progress: progress(message)
            code=run_process([info['python'],ROOT/'scripts/reanalysis_worker.py'],cancel,
                              timeout=3600 if download else 180,env=runtime_env(self.store.root),payload=payload,tick=tick)
            if code: raise ValueError('Обработчик реанализа завершился с ошибкой; частичное поле не используется.')
            try: result=json.loads((work/'result.json').read_text(encoding='utf-8'))
            except (OSError,ValueError): raise ValueError('Обработчик не вернул проверяемый результат.') from None
            if not result.get('ok'): raise ValueError(result.get('error','Поле не обработано.'))
            return result['result']

    def _field_start(self,payload,task_id=None,pending=None):
        self._fields_init()
        with self.lock:
            if self.busy: raise ValueError('Сначала завершите или отмените текущую операцию.')
            identity=task_id or uuid.uuid4().hex
            self._field_job={'id':identity,'status':'running','message':'Подготовка поля',
                             'pending':pending or {},'resume_available':False,'field_id':None}
            def update(message):
                with self.lock:
                    self._field_job['message']=message;atomic_json(self.fields_root/'job.json',self._field_job)
                self.log(message)
            def work():
                try:
                    network=payload['action']=='prepare' and not payload.get('path')
                    if network:
                        try: self.field(payload['identity']);network=False
                        except (ValueError,OSError): pass
                    result=self._field_process(payload,download=network,cancel=self.cancel,progress=update)
                    with self.lock:
                        self._field_job.update(status='done',field_id=result['field']['id'],message='Поле проверено и сохранено'+(' · использован кэш' if result.get('cached') else ''))
                        self._field_job['resume_available']=False
                    return {'reanalysis':result['field']}
                except Cancelled:
                    with self.lock: self._field_job.update(status='cancelled',message='Получение поля остановлено. Ранее готовые поля сохранены.',resume_available=False)
                    raise
                except Exception as exc:
                    with self.lock: self._field_job.update(status='error',message=str(exc) if isinstance(exc,ValueError) else 'Не удалось подготовить поле.',resume_available=False)
                    raise ValueError(self._field_job['message']) from None
                finally:
                    with self.lock: atomic_json(self.fields_root/'job.json',self._field_job)
            self.start('Поле реанализа',work)
            self._field_job=self._sanitize_field_job(self._field_job)
            atomic_json(self.fields_root/'job.json',self._field_job)
            return {'started':True,'id':identity}

    def fields_add(self,data,local=False):
        self._fields_init();plan=request_plan(data)
        credential={};path=None;identity=plan['key']
        if local:
            value=data.get('path','')
            if not isinstance(value,str) or not value or len(value)>4096: raise ValueError('Укажите полный путь к файлу на компьютере сервера.')
            path=Path(value).expanduser().resolve()
            if not path.is_file() or path.suffix.lower() not in ('.nc','.nc4','.netcdf','.grib','.grib2','.grb','.grb2'):
                raise ValueError('Выберите существующий NetCDF или GRIB.')
            if path.stat().st_size>1024**3: raise ValueError('Файл больше 1 ГиБ. Сначала выделите область и уровни.')
            identity=hashlib.sha256((identity+digest(path)).encode()).hexdigest()[:24]
        else:
            credential=self.source_credential(plan['source'])
            # Cached fields remain usable without credentials after a server restart.
            if not credential and not (self.field_cache/identity/'metadata.json').is_file():
                provider='cds' if plan['source'] in ('era5','carra2') else 'earthdata'
                with self.lock:
                    if self.busy: raise ValueError('Сначала завершите или отмените текущую операцию.')
                    task_id=uuid.uuid4().hex
                    action={'code':'credentials_required','provider':provider,'source':plan['source']}
                    pending={'kind':'remote','spec':self._safe_field_spec(plan)}
                    self._field_job={'id':task_id,'status':'needs_action','message':'Требуются данные доступа к источнику.',
                                     'needs_action':action,'pending':pending,'resume_available':True,'field_id':None}
                    atomic_json(self.fields_root/'job.json',self._sanitize_field_job(self._field_job))
                return {'needs_action':dict(action,id=task_id)}
        if sum(p.stat().st_size for p in self.field_cache.rglob('*') if p.is_file())>20*1024**3:
            raise ValueError('Кэш реанализа превысил 20 ГиБ. Удалите ненужные данные после резервирования.')
        pending={'kind':'remote','spec':self._safe_field_spec(plan)} if not local else {}
        return self._field_start({'action':'prepare','plan':plan,'credential':credential,
                                   'path':str(path) if path else None,'identity':identity,'cache':str(self.field_cache)},pending=pending)

    def fields_resume(self,data):
        """Resume the exact persisted remote request using credentials currently configured."""
        self._fields_init()
        identity=data.get('id') if isinstance(data,dict) else None
        with self.lock:
            job=self._field_job
            if not isinstance(identity,str) or not TASK_ID.fullmatch(identity) or identity!=job.get('id'):
                raise ValueError('Задача реанализа не найдена или уже заменена.')
            if job.get('status') not in ('needs_action','interrupted') or not job.get('resume_available'):
                raise ValueError('Эту задачу нельзя возобновить.')
            pending=job.get('pending',{})
            if pending.get('kind')!='remote': raise ValueError('Нет безопасно сохранённого плана удалённого запроса.')
            spec=self._safe_field_spec(pending.get('spec'))
            if not spec: raise ValueError('Сохранённый план задачи повреждён.')
        plan=request_plan(spec)
        credential=self.source_credential(plan['source'])
        cache_id=plan['key']
        if not credential and not (self.field_cache/cache_id/'metadata.json').is_file():
            provider='cds' if plan['source'] in ('era5','carra2') else 'earthdata'
            action={'code':'credentials_required','provider':provider,'source':plan['source']}
            with self.lock:
                # Recheck scope before updating in case a different job replaced it.
                if self._field_job.get('id')!=identity: raise ValueError('Задача уже заменена.')
                self._field_job.update(status='needs_action',message='Требуются данные доступа к источнику.',needs_action=action,resume_available=True)
                atomic_json(self.fields_root/'job.json',self._sanitize_field_job(self._field_job))
            return {'needs_action':dict(action,id=identity)}
        payload={'action':'prepare','plan':plan,'credential':credential,'path':None,'identity':cache_id,'cache':str(self.field_cache)}
        return self._field_start(payload,task_id=identity,pending={'kind':'remote','spec':spec})

    def fields_difference(self,data):
        a=self.field(data.get('first'));b=self.field(data.get('second'))
        if a['id']==b['id']: raise ValueError('Выберите два разных поля для A − B.')
        for key in ('variable','time','level','unit','time_kind'):
            if a.get(key)!=b.get(key): raise ValueError('Не совпадают поле, срок или уровень. Межвременная разность здесь не выполняется.')
        identity=hashlib.sha256((VERSION+'difference'+a['id']+a['sha256']+b['id']+b['sha256']).encode()).hexdigest()[:24]
        return self._field_start({'action':'difference','identity':identity,'cache':str(self.field_cache),
                                 'first':{'id':a['id'],'path':str(self.field_cache/a['id']/'field.nc')},
                                 'second':{'id':b['id'],'path':str(self.field_cache/b['id']/'field.nc')},'input_metadata':[a,b]})

    def fields_stack(self,data):
        self._fields_init();items=data.get('stack')
        if not isinstance(items,list) or len(items)>8: raise ValueError('Не более восьми слоёв в рабочем стеке.')
        result=[];seen=set()
        for row in items:
            if not isinstance(row,dict): raise ValueError('Неверная запись слоя.')
            meta=self.field(row.get('id'));key=meta['id']
            if key in seen: raise ValueError('В стеке повторяется одно поле.')
            seen.add(key);opacity=row.get('opacity',.65)
            if isinstance(opacity,bool) or not isinstance(opacity,(float,int)) or not math.isfinite(opacity) or not 0<=opacity<=1: raise ValueError('Прозрачность от 0 до 1.')
            styles=('fill','arrows','fill_arrows') if meta['wind'] else ('fill','contours','fill_contours')
            style=row.get('style','fill')
            if style not in styles or not isinstance(row.get('visible',True),bool): raise ValueError('Неверный способ отображения слоя.')
            result.append({'id':key,'opacity':opacity,'style':style,'visible':row.get('visible',True)})
        self.store.set_setting('reanalysis_stack',result)
        return {'stack':result}

    def fields_render(self,data):
        meta=self.field(data.get('id'));g=self.product_grid(self.product(data['product'])) if data.get('product') else grid(data.get('preset','arctic'),int(data.get('width',1000)))
        options={k:data[k] for k in ('min','max') if k in data}
        signature={'id':meta['id'],'sha256':meta['sha256'],'grid':{k:v for k,v in g.items() if k!='transform'},'options':options,'version':VERSION}
        key=hashlib.sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest()[:24]
        folder=self.field_renders/key;manifest=folder/'render.json'
        with self._field_render_lock:
            if not manifest.is_file():
                self._field_process({'action':'render','native':str(self.field_cache/meta['id']/'field.nc'),
                                     'grid':dict(g,transform=list(g['transform'])),'output':str(folder),'options':options})
            result=json.loads(manifest.read_text(encoding='utf-8'))
        return dict(result,id=meta['id'],render_id=key,image='/field-render/'+key+'/map.png')

    def field_image(self,identity):
        self._fields_init()
        if not ID.fullmatch(identity): raise ValueError('Неверный идентификатор изображения.')
        path=self.field_renders/identity/'map.png'
        if not (path.parent/'render.json').is_file(): raise ValueError('Изображение ещё не готово.')
        return path

    def fields_probe(self,data):
        self._fields_init()
        ids=data.get('ids',[])
        if not isinstance(ids,list) or len(ids)>8: raise ValueError('Не более восьми полей для анализа.')
        try: lon=float(data['lon']);lat=float(data['lat'])
        except (ValueError,TypeError,KeyError): raise ValueError('Нужны численные lon и lat.') from None
        if not math.isfinite(lon+lat) or not -180<=lon<=180 or not -90<=lat<=90: raise ValueError('Точка вне Земли.')
        if not ids: return {'rows':[],'lon':lon,'lat':lat}
        meta=[self.field(key) for key in ids]
        result=self._field_process({'action':'probe','fields':[{'id':m['id'],'path':str(self.field_cache/m['id']/'field.nc')} for m in meta],'lon':lon,'lat':lat})
        result['rows']=[dict({k:v for k,v in m.items() if k not in ('provenance','request')},**r) for m,r in zip(meta,result['rows'])]
        return dict(result,lon=lon,lat=lat)

    def fields_cancel(self,data):
        self._fields_init()
        with self.lock:
            job=self._field_job
            if data.get('id')!=job.get('id') or job.get('status') not in ('running','needs_action','interrupted','retrying'):
                raise ValueError('Эта задача уже не выполняется.')
            if job.get('status')=='running': self.cancel.set()
            else:
                job.update(status='cancelled',message='Задача отменена.');job['resume_available']=False
                atomic_json(self.fields_root/'job.json',self._sanitize_field_job(job))
        return {'cancelled':True}

    def fields_export(self,identity):
        meta=self.field(identity);result=io.BytesIO()
        with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.write(self.field_cache/identity/'field.nc','field.nc')
            archive.writestr('provenance.json',json.dumps(meta,ensure_ascii=False,indent=2))
        return result.getvalue()
