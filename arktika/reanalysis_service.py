"""API полей: задачи, проверяемый кэш, отображение и сохранённый стек слоёв."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import re
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path

from .download import atomic_json, digest
from .era5_runtime import ensure_runtime, run_process, runtime_env, runtime_info
from .geo import grid
from .model import iso, now
from .network import Cancelled, redact
from .pmc_archive import _times as pmc_archive_times
from .pmc_archive import _utc_hour as pmc_utc_hour
from .pmc_archive import build_plan as build_pmc_archive_plan
from .pmc_archive_run import MAX_FIELDS as MAX_PMC_ARCHIVE_FIELDS
from .pmc_archive_run import build_archive_report
from .pmc_archive_run import validate_grid as validate_pmc_archive_grid
from .reanalysis.adapters import get_runtime_adapter
from .reanalysis.catalog import SOURCES, VERSION, catalogue, request_plan

ROOT=Path(__file__).resolve().parents[1]
ID=re.compile(r'^[0-9a-f]{24}$')
TASK_ID=re.compile(r'^[0-9a-f]{32}$')


class ReanalysisMixin:
    @staticmethod
    def _read_pmc_mask_raster(data, label):
        """Read one bounded, single-band categorical GeoTIFF mask."""
        import numpy as np
        from rasterio.io import MemoryFile
        from pyproj import CRS

        if not isinstance(data,bytes) or not data or len(data)>32*1024*1024:
            raise ValueError(f'{label}: нужен непустой GeoTIFF размером не более 32 МиБ.')
        try:
            with MemoryFile(data) as mem, mem.open() as ds:
                if ds.driver!='GTiff' or ds.count!=1:
                    raise ValueError(f'{label}: ожидается одноканальный GeoTIFF.')
                if ds.width<1 or ds.height<1 or ds.width*ds.height>16_000_000:
                    raise ValueError(f'{label}: размер растра вне допустимых пределов.')
                if ds.crs is None or not all(math.isfinite(v) for v in ds.transform[:6]):
                    raise ValueError(f'{label}: в растре отсутствует корректная сетка или CRS.')
                crs=CRS.from_user_input(ds.crs)
                if (not crs.is_projected or not crs.axis_info
                        or any(abs(float(axis.unit_conversion_factor or 0)-1.0)>1e-9
                               for axis in crs.axis_info[:2])):
                    raise ValueError(f'{label}: требуется проецированная сетка с осями в метрах.')
                if abs(ds.transform.a*ds.transform.e-ds.transform.b*ds.transform.d)<1e-15:
                    raise ValueError(f'{label}: преобразование геосетки вырождено.')
                if ds.nodata is not None:
                    raise ValueError(f'{label}: nodata должен задаваться отдельной маской покрытия.')
                if not np.all(ds.read_masks(1)==255):
                    raise ValueError(f'{label}: внутренние маски/alpha с пропусками запрещены; неизвестные ячейки задаются coverage.tif.')
                if ds.dtypes[0] not in ('uint8','uint16','int8','int16','uint32','int32'):
                    raise ValueError(f'{label}: метки должны быть целочисленными.')
                values=ds.read(1)
                if not np.isin(values,(0,1)).all():
                    raise ValueError(f'{label}: допустимы только значения 0 и 1.')
                grid={'crs':ds.crs.to_string(),'width':int(ds.width),'height':int(ds.height),
                      'transform':[float(v) for v in ds.transform[:6]],
                      'resolution_x_m':float(math.hypot(ds.transform.a,ds.transform.d)),
                      'resolution_y_m':float(math.hypot(ds.transform.b,ds.transform.e))}
                return values.astype(np.uint8,copy=False),grid
        except ValueError:
            raise
        except Exception as exc:
            # Rasterio rejects HTML/XML error pages and malformed uploads here.
            raise ValueError(f'{label}: файл не является читаемым GeoTIFF.') from exc

    def register_pmc_observation(self, labels_bytes: bytes, coverage_bytes: bytes, metadata: dict) -> dict:
        """Validate and persist an explicitly attributed satellite label/coverage pair.

        This stores masks only. It does not assess meteorological correctness or
        calculate a detection score.
        """
        import numpy as np
        from datetime import datetime,timezone

        allowed={'schema_version','observation_ref','source','product','product_version',
                 'valid_time','label_convention','qc_status','grid','nodata'}
        if not isinstance(metadata,dict) or set(metadata)!=allowed:
            raise ValueError('Метаданные наблюдения должны содержать ровно определённые поля sidecar.')
        if metadata.get('schema_version')!='arktika.pmc_satellite_observation.v1':
            raise ValueError('Неизвестная версия sidecar наблюдения.')
        id_pattern=re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$')
        for key in ('observation_ref','source','product','product_version'):
            value=metadata.get(key)
            if not isinstance(value,str) or not id_pattern.fullmatch(value):
                raise ValueError(f'Некорректное поле метаданных {key}.')
        if metadata.get('label_convention')!='0=no_pmc,1=pmc_candidate':
            raise ValueError('Соглашение меток должно явно задавать 0=no_pmc,1=pmc_candidate.')
        if metadata.get('qc_status') not in ('not_assessed','user_reviewed'):
            raise ValueError('qc_status может быть только not_assessed или user_reviewed.')
        if metadata.get('nodata') is not None:
            raise ValueError('Для раздельной маски покрытия nodata в GeoTIFF и sidecar должен быть null.')
        raw_time=metadata.get('valid_time')
        if not isinstance(raw_time,str) or not re.search(r'(?:Z|\+00:00)$',raw_time):
            raise ValueError('valid_time должен содержать явное смещение UTC.')
        try:
            stamp=datetime.fromisoformat(raw_time.replace('Z','+00:00'))
            if stamp.utcoffset()!=timezone.utc.utcoffset(stamp):raise ValueError()
            valid_time=stamp.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
        except (ValueError,OverflowError):
            raise ValueError('Некорректный срок UTC наблюдения.') from None
        labels,label_grid=self._read_pmc_mask_raster(labels_bytes,'Маска меток')
        coverage,coverage_grid=self._read_pmc_mask_raster(coverage_bytes,'Маска покрытия')
        if label_grid!=coverage_grid:
            raise ValueError('Маски меток и покрытия должны иметь одинаковые сетку и CRS.')
        if np.any((labels==1)&(coverage==0)):
            raise ValueError('Положительные метки не могут находиться вне маски покрытия.')
        declared=metadata.get('grid')
        if not isinstance(declared,dict) or set(declared)!={'crs','width','height','transform','resolution_x_m','resolution_y_m'}:
            raise ValueError('В sidecar требуется полное описание сетки.')
        try:
            if (isinstance(declared.get('width'),bool) or not isinstance(declared.get('width'),int)
                    or isinstance(declared.get('height'),bool) or not isinstance(declared.get('height'),int)
                    or not isinstance(declared.get('transform'),list)):
                raise ValueError()
            checked={'crs':str(declared['crs']),'width':declared['width'],
                     'height':declared['height'],'transform':[float(x) for x in declared['transform']],
                     'resolution_x_m':float(declared['resolution_x_m']),
                     'resolution_y_m':float(declared['resolution_y_m'])}
        except (TypeError,ValueError):
            raise ValueError('Некорректное описание сетки в sidecar.') from None
        if (isinstance(declared['width'],bool) or isinstance(declared['height'],bool)
                or len(checked['transform'])!=6
                or any(not math.isfinite(v) for v in checked['transform']+[checked['resolution_x_m'],checked['resolution_y_m']])
                or checked!=label_grid):
            raise ValueError('Сетка sidecar должна точно совпадать с обеими масками.')
        safe={key:metadata[key] for key in allowed if key not in ('grid','valid_time')}
        safe.update(valid_time=valid_time,grid=label_grid,units='1',algorithm_id='arktika.pmc_satellite_observation_import',
                    algorithm_version='0.1.0',status='imported_unverified')
        source_digest=hashlib.sha256(labels_bytes+coverage_bytes).hexdigest()
        safe['source_files_sha256']=source_digest
        payload=io.BytesIO()
        np.savez_compressed(payload,labels=labels,coverage=coverage,
                            metadata=np.frombuffer(json.dumps(safe,ensure_ascii=False,separators=(',',':')).encode('utf-8'),dtype=np.uint8))
        data=payload.getvalue();checksum=hashlib.sha256(data).hexdigest()
        self._fields_init()
        with self.lock:
            root=self.fields_root/'pmc-observations';root.mkdir(mode=0o700,exist_ok=True)
            try:root.chmod(0o700)
            except OSError:pass
            identity=uuid.uuid4().hex[:24]
            target=root/(identity+'.npz');tmp=root/(identity+'.tmp');hash_target=root/(identity+'.sha256');hash_tmp=root/(identity+'.sha256.tmp')
            try:
                with hash_tmp.open('x',encoding='ascii') as stream:
                    stream.write(checksum+'\n');stream.flush()
                try:hash_tmp.chmod(0o600)
                except OSError:pass
                hash_tmp.replace(hash_target)
                with tmp.open('xb') as stream:
                    stream.write(data);stream.flush()
                try:tmp.chmod(0o600)
                except OSError:pass
                tmp.replace(target)
            finally:
                try:tmp.unlink()
                except FileNotFoundError:pass
                try:hash_tmp.unlink()
                except FileNotFoundError:pass
        observation=dict(safe,observation_id=identity,sha256=checksum)
        return {'observation_id':identity,'observation':observation,'sha256':checksum,'status':'imported_unverified'}

    def pmc_observation(self, identity):
        """Load a previously registered mask pair after checking its archive hash."""
        import numpy as np

        if not isinstance(identity,str) or not ID.fullmatch(identity):
            raise ValueError('Неверный идентификатор спутникового наблюдения.')
        self._fields_init();path=self.fields_root/'pmc-observations'/(identity+'.npz')
        try:
            data=path.read_bytes()
            expected=(path.with_suffix('.sha256')).read_text(encoding='ascii').strip()
        except OSError:raise ValueError('Спутниковое наблюдение не найдено в локальном хранилище.') from None
        checksum=hashlib.sha256(data).hexdigest()
        if not re.fullmatch(r'[0-9a-f]{64}',expected) or checksum!=expected:
            raise ValueError('Контрольная сумма спутникового наблюдения не совпадает.')
        try:
            with np.load(io.BytesIO(data),allow_pickle=False) as archive:
                if set(archive.files)!= {'labels','coverage','metadata'}:raise ValueError()
                labels=archive['labels'];coverage=archive['coverage']
                metadata=json.loads(archive['metadata'].tobytes().decode('utf-8'))
        except Exception:
            raise ValueError('Сохранённая маска повреждена.') from None
        if (labels.ndim!=2 or labels.shape!=coverage.shape or labels.dtype!=np.uint8
                or coverage.dtype!=np.uint8 or not np.isin(labels,(0,1)).all()
                or not np.isin(coverage,(0,1)).all()
                or not isinstance(metadata,dict) or metadata.get('status')!='imported_unverified'):
            raise ValueError('Сохранённая маска не прошла проверку целостности.')
        if not isinstance(metadata.get('product'),str):
            raise ValueError('Сохранённый паспорт наблюдения не содержит идентификатор продукта.')
        return labels,coverage,dict(metadata,observation_id=identity,
                                    product_id=metadata['product'],sha256=checksum)

    def pmc_observations_state(self):
        """List only verified local observation summaries; never expose paths or arrays."""
        self._fields_init();root=self.fields_root/'pmc-observations';items=[]
        for path in root.glob('*.npz') if root.is_dir() else ():
            identity=path.stem
            if not ID.fullmatch(identity):continue
            try:
                _,_,metadata=self.pmc_observation(identity)
                items.append({key:metadata[key] for key in
                              ('observation_id','source','product','product_id','valid_time','status')})
            except (OSError,ValueError,KeyError):
                continue
        items.sort(key=lambda item:(item['valid_time'],item['observation_id']))
        return {'observations':items}

    def _adapter_credential(self, source):
        """Use a registered adapter's shared provider without storing secrets in its state."""
        provider=get_runtime_adapter(source).auth_provider
        if provider=='cds': return self.source_credential('era5')
        if provider=='earthdata': return self.source_credential('merra2')
        if provider=='none': return {}
        raise ValueError('Для этого адаптера не подключён провайдер реквизитов.')

    def _fields_init(self):
        self._sources_init()
        with self.lock:
            if hasattr(self,'fields_root'): return
            self.fields_root=self.store.root/'reanalysis';self.fields_root.mkdir(exist_ok=True)
            self.field_cache=self.fields_root/'fields';self.field_cache.mkdir(exist_ok=True)
            self.field_renders=self.fields_root/'renders';self.field_renders.mkdir(exist_ok=True)
            self._field_render_lock=threading.Lock()
            self.pmc_archive_root=self.fields_root/'pmc-archives';self.pmc_archive_root.mkdir(exist_ok=True)
            try: self._field_job=json.loads((self.fields_root/'job.json').read_text(encoding='utf-8'))
            except (ValueError,OSError): self._field_job={'status':'idle'}
            if self._field_job.get('status')=='running':
                self._field_job.update(status='interrupted',message='Операция прервана остановкой сервера. Готовые поля сохранены; задачу можно возобновить.')
                self._field_job['resume_available']=bool(self._field_job.get('pending',{}).get('kind')=='remote')
            # Older job records may have contained request payloads. Keep only the
            # allowlisted public task summary before exposing or rewriting them.
            self._field_job=self._sanitize_field_job(self._field_job)
            atomic_json(self.fields_root/'job.json',self._field_job)
            try:self._pmc_archive_job=json.loads((self.fields_root/'pmc_archive_job.json').read_text(encoding='utf-8'))
            except (ValueError,OSError):self._pmc_archive_job={'status':'idle'}
            self._pmc_archive_job=self._sanitize_pmc_archive_job(self._pmc_archive_job)
            if self._pmc_archive_job.get('status')=='running':
                self._pmc_archive_job.update(status='interrupted',message='Архивный расчёт прерван остановкой сервера. Завершённые сроки сохранены.',resume_available=True)
                atomic_json(self.fields_root/'pmc_archive_job.json',self._pmc_archive_job)

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

    def pmc_archive_plan(self, data):
        """Describe archival inputs without downloading or fabricating a result."""
        state=self.fields_state()
        sources=data.get('sources',[]) if isinstance(data,dict) else []
        credentials={source:bool(self.source_credential(source)) for source in sources
                     if source in ('era5','carra2','merra2')}
        return build_pmc_archive_plan(data,credentials=credentials,cached_fields=state['fields'])

    def _pmc_archive_task_path(self, identity):
        if not isinstance(identity,str) or not TASK_ID.fullmatch(identity):
            raise ValueError('Неверный идентификатор архивной задачи.')
        return self.pmc_archive_root/identity

    def _pmc_archive_frame_records(self, job):
        folder=self._pmc_archive_task_path(job.get('id'))/'frames'
        fields={item.get('id'):item for item in self.fields_state().get('fields',[])}
        records=[]
        for identity in job.get('field_ids',[]):
            if not isinstance(identity,str) or not ID.fullmatch(identity):continue
            path=folder/(identity+'.json')
            try:record=json.loads(path.read_text(encoding='utf-8'))
            except (OSError,ValueError):continue
            if not isinstance(record,dict) or record.get('field_id')!=identity:continue
            meta=fields.get(identity)
            if not isinstance(meta,dict) or (record.get('source'),record.get('valid_time'))!=(meta.get('source'),meta.get('time')):
                continue
            if record.get('status')=='error':
                if isinstance(record.get('error'),str):records.append(record)
                continue
            result=record.get('result')
            if record.get('status')!='completed' or not isinstance(result,dict):continue
            if (result.get('algorithm_id')!='arktika.experimental_pmc_candidates'
                    or result.get('algorithm_version')!='0.2.0'
                    or result.get('source')!=meta.get('source')
                    or result.get('valid_time')!=meta.get('time')
                    or result.get('grid')!=job.get('target_grid')
                    or not isinstance(result.get('candidates'),list)
                    or result.get('candidate_count')!=len(result['candidates'])):continue
            valid=True
            for candidate in result['candidates']:
                rc=candidate.get('centroid_pixel_rc') if isinstance(candidate,dict) else None
                if (not isinstance(rc,list) or len(rc)!=2
                        or any(isinstance(value,bool) or not isinstance(value,(int,float)) for value in rc)
                        or not 0<=rc[0]<job['target_grid']['height']
                        or not 0<=rc[1]<job['target_grid']['width']
                        or candidate.get('classification')!='candidate'
                        or candidate.get('confirmation_status')!='not_confirmed'):
                    valid=False;break
            if valid:records.append(record)
        return records

    @staticmethod
    def _sanitize_pmc_archive_job(job):
        """Load only validated, non-secret parameters from the durable task file."""
        if not isinstance(job,dict):return {'status':'idle'}
        identity=job.get('id');status=job.get('status')
        if (not isinstance(identity,str) or not TASK_ID.fullmatch(identity)
                or status not in ('running','interrupted','cancelled','error','partial','completed')):
            return {'status':'idle','message':'Сохранённая архивная задача повреждена.'}
        fields=job.get('field_ids');plan=job.get('plan')
        try:
            if (not isinstance(fields,list) or not fields or len(fields)>MAX_PMC_ARCHIVE_FIELDS
                    or any(not isinstance(item,str) or not ID.fullmatch(item) for item in fields)
                    or len(set(fields))!=len(fields)):
                raise ValueError()
            if not isinstance(plan,dict) or set(plan)-{'sources','start','end','area','step_count'} or not {'sources','start','end','area'}<=set(plan):
                raise ValueError()
            if (not isinstance(plan['sources'],list) or not plan['sources']
                    or any(source not in SOURCES for source in plan['sources'])
                    or len(set(plan['sources']))!=len(plan['sources'])):
                raise ValueError()
            pmc_utc_hour(plan['start'],'Начало');pmc_utc_hour(plan['end'],'Конец')
            if not isinstance(plan['area'],list) or len(plan['area'])!=4:
                raise ValueError()
            request={'sources':plan['sources'],'start':plan['start'],'end':plan['end'],
                     'area':[float(x) for x in plan['area']]}
            build_pmc_archive_plan(request,credentials={},cached_fields=[])
            target_grid=validate_pmc_archive_grid(job.get('target_grid'))
        except (ValueError,TypeError,KeyError):
            return {'status':'idle','message':'Сохранённая архивная задача повреждена.'}
        return {'id':identity,'status':status,'message':str(job.get('message',''))[:500],
                'created_at':str(job.get('created_at',''))[:64],
                'updated_at':str(job.get('updated_at',''))[:64],
                'plan':{'sources':list(plan['sources']),'start':plan['start'],'end':plan['end'],
                        'area':[float(x) for x in plan['area']]},
                'field_ids':list(fields),'target_grid':target_grid,'total':len(fields),
                'resume_available':bool(job.get('resume_available'))}

    def _pmc_archive_report_snapshot(self,job):
        identity=job['id'];task_dir=self._pmc_archive_task_path(identity)
        expected={source:[stamp.isoformat().replace('+00:00','Z') for stamp in pmc_archive_times(
            pmc_utc_hour(job['plan']['start'],'Начало'),pmc_utc_hour(job['plan']['end'],'Конец'),source)]
            for source in job['plan']['sources']}
        records=self._pmc_archive_frame_records(job)
        return build_archive_report(task_id=identity,plan=job['plan'],expected_times=expected,
            interval_hours={source:SOURCES[source]['step_hours'] for source in job['plan']['sources']},
            frame_records=records,cached_fields=self.fields_state()['fields'],
            target_grid=job.get('target_grid'))

    def pmc_archive_state(self, data=None):
        self._fields_init()
        with self.lock:job=copy.deepcopy(self._pmc_archive_job)
        identity=job.get('id')
        if not isinstance(identity,str) or not TASK_ID.fullmatch(identity):
            return {'status':'idle','message':'Архивная задача ещё не запускалась.'}
        records=self._pmc_archive_frame_records(job)
        return {k:copy.deepcopy(job[k]) for k in (
            'id','status','message','created_at','updated_at','total','resume_available'
        ) if k in job} | {'completed':sum(row.get('status')=='completed' for row in records),
                          'failed':sum(row.get('status')=='error' for row in records),
                          'processed':len(records)}

    def _persist_pmc_archive_job(self):
        atomic_json(self.fields_root/'pmc_archive_job.json',self._pmc_archive_job)

    def _run_pmc_archive_task(self, identity):
        task_dir=self._pmc_archive_task_path(identity);frames_dir=task_dir/'frames';frames_dir.mkdir(parents=True,exist_ok=True)
        with self.lock:job=copy.deepcopy(self._pmc_archive_job)
        try:
            completed_ids={row.get('field_id') for row in self._pmc_archive_frame_records(job)
                           if row.get('status')=='completed'}
            for index,field_id in enumerate(job['field_ids'],start=1):
                if self.cancel.is_set():raise Cancelled()
                frame_path=frames_dir/(field_id+'.json')
                if field_id in completed_ids:continue
                meta=self.field(field_id)
                with self.lock:
                    self._pmc_archive_job.update(status='running',message=f"Срок {index} из {job['total']}: {meta.get('source')} · {meta.get('time')}",resume_available=False)
                    self._persist_pmc_archive_job()
                try:
                    result=self.experimental_pmc_candidates(field_id,target_grid=job['target_grid'],
                                                           region_bounds=job['plan']['area'])
                    record={'field_id':field_id,'source':meta['source'],'valid_time':meta['time'],
                            'status':'completed','result':result}
                except Cancelled:raise
                except Exception as exc:
                    record={'field_id':field_id,'source':meta.get('source'),'valid_time':meta.get('time'),
                            'status':'error','error':redact(exc)}
                if self.cancel.is_set():raise Cancelled()
                atomic_json(frame_path,record)
                if record.get('status')=='completed':completed_ids.add(field_id)
                with self.lock:
                    self._pmc_archive_job['updated_at']=iso(now())
                    self._persist_pmc_archive_job()

            if self.cancel.is_set():raise Cancelled()
            report=self._pmc_archive_report_snapshot(job)
            atomic_json(task_dir/'report.json',report)
            records=self._pmc_archive_frame_records(job)
            recorded_ids={row.get('field_id') for row in records}
            has_errors=(any(row.get('status')=='error' for row in records)
                        or bool(set(job['field_ids'])-recorded_ids))
            with self.lock:
                self._pmc_archive_job.update(status=report['status'],message=(
                    'Архив обработан полностью.' if report['status']=='completed'
                    else 'Архив обработан частично; пропуски и ошибки перечислены в отчёте.'),
                    resume_available=has_errors,updated_at=iso(now()))
                self._persist_pmc_archive_job()
        except Cancelled:
            report=self._pmc_archive_report_snapshot(job)
            atomic_json(task_dir/'report.json',report)
            with self.lock:
                self._pmc_archive_job.update(status='cancelled',message='Расчёт отменён. Завершённые сроки сохранены.',
                                             resume_available=True,updated_at=iso(now()))
                self._persist_pmc_archive_job()
        except Exception as exc:
            with self.lock:
                self._pmc_archive_job.update(status='error',message=redact(exc),resume_available=True,updated_at=iso(now()))
                self._persist_pmc_archive_job()
            raise ValueError(redact(exc)) from None

    def pmc_archive_start(self,data):
        self._fields_init()
        if not isinstance(data,dict) or set(data)!={'plan','field_ids','target_grid'}:
            raise ValueError('Архивный запуск принимает plan, явный список field_ids и target_grid.')
        planned=self.pmc_archive_plan(data['plan']);plan=planned['plan']
        ids=data['field_ids']
        if not isinstance(ids,list) or not ids or len(ids)>MAX_PMC_ARCHIVE_FIELDS:
            raise ValueError(f"Выберите от 1 до {MAX_PMC_ARCHIVE_FIELDS} кэшированных сроков.")
        if any(not isinstance(item,str) or not ID.fullmatch(item) for item in ids) or len(set(ids))!=len(ids):
            raise ValueError('Список кэшированных field_id некорректен или содержит повторы.')
        target_grid=validate_pmc_archive_grid(data['target_grid'])
        start=pmc_utc_hour(plan['start'],'Начало');end=pmc_utc_hour(plan['end'],'Конец')
        scheduled={source:{stamp.isoformat().replace('+00:00','Z') for stamp in pmc_archive_times(start,end,source)}
                   for source in plan['sources']}
        metas=[self.field(item) for item in ids]
        seen=set()
        for meta in metas:
            source,time=meta.get('source'),meta.get('time')
            if source not in ('era5','carra2','merra2'):
                raise ValueError('Архивный детектор поддерживает только ERA5, CARRA2 и MERRA-2.')
            if source not in scheduled or time not in scheduled[source]:
                raise ValueError('Каждое поле должно принадлежать выбранному источнику и сроку UTC плана.')
            if meta.get('variable')!='wind' or meta.get('level')!=850 or meta.get('time_kind')!='instantaneous_analysis':
                raise ValueError('Архивный детектор принимает только мгновенный ветер 850 гПа.')
            if (source,time) in seen:raise ValueError('Для источника и срока выбрано несколько полей; оставьте одно поле на срок.')
            seen.add((source,time))
        with self.lock:
            if self.busy:raise ValueError('Сначала завершите текущую операцию.')
            if self._pmc_archive_job.get('status')=='running':raise ValueError('Архивная задача уже выполняется.')
            identity=uuid.uuid4().hex
            job={'id':identity,'status':'running','message':'Подготовка архивного расчёта',
                 'created_at':iso(now()),'updated_at':iso(now()),'plan':copy.deepcopy(plan),
                 'field_ids':list(ids),'target_grid':target_grid,'total':len(ids),'resume_available':False}
            self._pmc_archive_job=job;self._persist_pmc_archive_job()
            self.start('Архивный анализ кандидатов ПМЦ',lambda:self._run_pmc_archive_task(identity))
        return {'started':True,'id':identity,'status':'running','selected_steps':len(ids),
                'expected_steps':planned['plan']['step_count']}

    def pmc_archive_resume(self,data):
        self._fields_init();identity=data.get('id') if isinstance(data,dict) else None
        with self.lock:job=copy.deepcopy(self._pmc_archive_job)
        if identity!=job.get('id') or job.get('status') not in ('interrupted','cancelled','error','partial') or not job.get('resume_available'):
            raise ValueError('Эту архивную задачу нельзя возобновить.')
        for field_id in job.get('field_ids',[]):self.field(field_id)
        with self.lock:
            if self.busy:raise ValueError('Сначала завершите текущую операцию.')
            self._pmc_archive_job.update(status='running',message='Возобновление с последнего сохранённого срока.',resume_available=False)
            self._persist_pmc_archive_job()
            self.start('Продолжение архивного анализа ПМЦ',lambda:self._run_pmc_archive_task(identity))
        return {'started':True,'id':identity,'status':'running'}

    def pmc_archive_cancel(self,data):
        self._fields_init()
        with self.lock:
            if data.get('id')!=self._pmc_archive_job.get('id') or self._pmc_archive_job.get('status')!='running':
                raise ValueError('Активная архивная задача не найдена.')
            self.cancel.set()
        return {'cancelled':True}

    def pmc_archive_report(self,identity):
        task_dir=self._pmc_archive_task_path(identity)
        path=task_dir/'report.json'
        with self.lock:job=copy.deepcopy(self._pmc_archive_job)
        if job.get('id')==identity and job.get('status') in ('running','interrupted','cancelled','error'):
            report=self._pmc_archive_report_snapshot(job)
            atomic_json(path,report)
        try:return path.read_bytes()
        except OSError:raise ValueError('Отчёт архивного расчёта ещё не готов.') from None

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
            try: provider=get_runtime_adapter(source).auth_provider
            except ValueError: provider=None
            if provider in ('cds','earthdata'):
                action={'code':'credentials_required','provider':provider,'source':source}
        if result.get('status') in ('needs_action','interrupted') and isinstance(action,dict):
            source=action.get('source')
            try: provider=get_runtime_adapter(source).auth_provider
            except ValueError: provider=None
            if action.get('code')=='credentials_required' and provider in ('cds','earthdata') and action.get('provider')==provider:
                result['needs_action']={'code':'credentials_required','provider':provider,'source':source}
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
            adapter=get_runtime_adapter(plan['source'])
            credential=self._adapter_credential(plan['source'])
            # Cached fields remain usable without credentials after a server restart.
            if adapter.auth_provider!='none' and not credential and not (self.field_cache/identity/'metadata.json').is_file():
                provider=adapter.auth_provider
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
        adapter=get_runtime_adapter(plan['source'])
        credential=self._adapter_credential(plan['source'])
        cache_id=plan['key']
        if adapter.auth_provider!='none' and not credential and not (self.field_cache/cache_id/'metadata.json').is_file():
            provider=adapter.auth_provider
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
