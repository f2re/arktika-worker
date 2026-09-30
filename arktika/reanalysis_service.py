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
                self._field_job.update(status='interrupted',message='Операция прервана остановкой сервера. Готовые поля сохранены; повторите запрос.')

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

    def _field_start(self,payload):
        self._fields_init()
        with self.lock:
            if self.busy: raise ValueError('Сначала завершите или отмените текущую операцию.')
            identity=uuid.uuid4().hex
            self._field_job={'id':identity,'status':'running','message':'Подготовка поля',
                             'request':payload.get('plan',{}),'field_id':None}
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
                    return {'reanalysis':result['field']}
                except Cancelled:
                    with self.lock: self._field_job.update(status='cancelled',message='Получение поля остановлено. Ранее готовые поля сохранены.')
                    raise
                except Exception as exc:
                    with self.lock: self._field_job.update(status='error',message=str(exc) if isinstance(exc,ValueError) else 'Не удалось подготовить поле.')
                    raise ValueError(self._field_job['message']) from None
                finally:
                    with self.lock: atomic_json(self.fields_root/'job.json',self._field_job)
            self.start('Поле реанализа',work)
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
                raise ValueError('Настройте доступ к '+plan['source']+' в «Источниках данных».')
        if sum(p.stat().st_size for p in self.field_cache.rglob('*') if p.is_file())>20*1024**3:
            raise ValueError('Кэш реанализа превысил 20 ГиБ. Удалите ненужные данные после резервирования.')
        return self._field_start({'action':'prepare','plan':plan,'credential':credential,
                                   'path':str(path) if path else None,'identity':identity,'cache':str(self.field_cache)})

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
            if data.get('id')!=self._field_job.get('id') or self._field_job.get('status')!='running': raise ValueError('Эта задача уже не выполняется.')
            self.cancel.set()
        return {'cancelled':True}

    def fields_export(self,identity):
        meta=self.field(identity);result=io.BytesIO()
        with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.write(self.field_cache/identity/'field.nc','field.nc')
            archive.writestr('provenance.json',json.dumps(meta,ensure_ascii=False,indent=2))
        return result.getvalue()
