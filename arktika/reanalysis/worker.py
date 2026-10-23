"""Изолированное выполнение: реквизиты только из stdin, результаты без секретов."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from ..download import atomic_json, digest
from ..product_schema import product_passport
from .adapters import (
    DEFAULT_REGISTRY,
    AdapterRegistry,
    get_runtime_adapter,
    load_installed_adapters,
)
from .catalog import SOURCES, VARIABLES, VERSION
from .normalize import save_native
from .transport import AccessError, combine_parts, data_files


def _grid_passport(native):
    import numpy as np
    values=native["value"]
    latitude=np.asarray(native["latitude"].values,dtype="<f8")
    longitude=np.asarray(native["longitude"].values,dtype="<f8")
    if values.ndim!=2 or latitude.shape!=values.shape or longitude.shape!=values.shape:
        raise ValueError("Адаптер должен вернуть значение и 2-D геолокацию одной формы.")
    digest=hashlib.sha256(latitude.tobytes(order="C")+longitude.tobytes(order="C")).hexdigest()
    regular=(np.all(latitude==latitude[:,:1]) and np.all(longitude==longitude[:1,:]))
    return {"crs":str(native.attrs.get("geolocation_crs","EPSG:4326")),
            "width":int(values.shape[1]),"height":int(values.shape[0]),
            "grid_type":"regular" if regular else "curvilinear",
            "geolocation_sha256":digest,
            "coordinate_variables":["latitude","longitude"]}


def _safe_provenance(provenance):
    """Persist a small audit record; never store arbitrary provider URLs or secrets."""
    allowed={"kind","sha256","dataset","version","variables","area","transport",
             "granule_id","source_attribution","schema_id","operation"}
    if not isinstance(provenance,list): return []
    result=[]
    for record in provenance:
        if not isinstance(record,dict): continue
        clean={}
        for key in allowed:
            value=record.get(key)
            if key in ("variables","area") and isinstance(value,list):
                values=[v for v in value if isinstance(v,(str,int,float)) and not isinstance(v,bool)]
                if all(not isinstance(v,str) or not re.search(r"[a-z][a-z0-9+.-]*://|token|password|secret|api[_-]?key",v,re.I) for v in values): clean[key]=values
            elif isinstance(value,(str,int,float)) and not isinstance(value,bool):
                if not isinstance(value,str) or not re.search(r"[a-z][a-z0-9+.-]*://|token|password|secret|api[_-]?key|authorization",value,re.I):
                    clean[key]=str(value)[:240] if isinstance(value,str) else value
        if clean: result.append(clean)
    return result


def metadata(native,identity,plan,provenance,adapter):
    import numpy as np
    source=native.attrs['source'];variable=native.attrs['variable'];grid=_grid_passport(native)
    difference_of=json.loads(native.attrs.get('difference_of','null'))
    values=np.asarray(native['value'].values)
    dataset_ids=[]
    for item in plan.get('requests',[]) if isinstance(plan,dict) else []:
        dataset=item.get('dataset') if isinstance(item,dict) else None
        if isinstance(dataset,str) and dataset not in dataset_ids:dataset_ids.append(dataset)
    variable_spec=adapter.variable_spec(variable)
    if variable_spec is None:
        raise ValueError('Каталог адаптера не описывает сохранённое поле.')
    derived=bool(difference_of)
    product_unit=native['value'].attrs.get('units',variable_spec['unit']) if derived else variable_spec['unit']
    algorithm_id='arktika.reanalysis.difference' if derived else native.attrs['adapter_id']
    algorithm_version=VERSION if derived else native.attrs['adapter_version']
    maturity='implemented' if derived else native.attrs['adapter_maturity']
    product_id=f"reanalysis.difference.{variable}" if derived else f"reanalysis.{source}.{variable}"
    product_sources=[]
    if derived:
        for item in provenance[0].get('inputs',[]) if provenance and isinstance(provenance[0],dict) else []:
            if isinstance(item,dict) and isinstance(item.get('source'),str):
                product_sources.append({'source_id':item['source'],'field_id':item.get('id','')})
    if not product_sources:
        product_sources=[{"source_id":source,"dataset_ids":dataset_ids,"adapter_id":native.attrs['adapter_id']}]
    passport=product_passport(
        product_id=product_id,
        algorithm_id=algorithm_id,
        algorithm_version=algorithm_version,
        maturity=maturity,
        quantity_kind="physical_quantity_difference" if derived else "physical_quantity",
        quantity=f"difference of {variable}" if derived else variable,units=product_unit,
        valid_time=native.attrs['time'],
        data_kind={"instantaneous_analysis":"analysis","analysis":"analysis","observation":"observation"}[native.attrs['time_kind']],
        grid=grid,source_grids=[dict(grid)],
        nodata="NaN; невалидные и отсутствующие ячейки сохраняются как unknown",
        quality_control={"status":"passed_normalization","time_kind":native.attrs['time_kind'],
                         "valid_cells":int(np.count_nonzero(np.isfinite(values))),
                         "missing_cells":int(np.count_nonzero(~np.isfinite(values)))},
        sources=product_sources,
        calibration_version="not_applicable",calibration_status="not_applicable",
        limitations=["Паспорт подтверждает техническую нормализацию, но сам по себе не подтверждает независимую метеорологическую точность источника."])
    return {'id':identity,'source':source,'variable':variable,'passport':passport,
            'adapter_id':algorithm_id,'adapter_version':algorithm_version,
            'title':('Разность A−B: ' if derived else '')+variable_spec['title'],
            'unit':product_unit if derived else native.attrs['unit'],
            'time':native.attrs['time'],'level':json.loads(native.attrs['level']),
            'time_kind':native.attrs['time_kind'],'resolution_km':native.attrs['resolution_km'],
            'area':json.loads(native.attrs['area']),'version':VERSION,
            'shape':list(native.value.shape),'wind':bool('u' in native),
            'provenance':_safe_provenance(provenance),'request':plan,
            'difference_of':difference_of,
            'source_label':native.attrs.get('source_label',SOURCES.get(native.attrs['source'],{}).get('name',native.attrs['source'])),
            'created_at':dt.datetime.now(dt.timezone.utc).isoformat()}


def checked_cache(folder):
    try:
        meta=json.loads((folder/'metadata.json').read_text(encoding='utf-8'))
        if digest(folder/'field.nc')==meta['sha256']: return meta
    except (OSError,ValueError,KeyError): pass
    return None


def prepare(payload,progress,*,registry: AdapterRegistry = DEFAULT_REGISTRY):
    import xarray as xr
    plan=payload['plan'];cache=Path(payload['cache']);identity=payload['identity'];target=cache/identity
    adapter=get_runtime_adapter(plan.get('source'),registry=registry)
    if plan.get('variable') not in adapter.supported_variables:
        raise ValueError('Выбранное поле не поддерживается источником реанализа.')
    if not payload.get('refresh'):
        old=checked_cache(target)
        if old: return {'field':old,'cached':True}
    cache.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(cache).free<128*1024**2: raise ValueError('Для обработки требуется не менее 128 МиБ свободного места.')
    with tempfile.TemporaryDirectory(prefix='.field-',dir=cache) as temporary:
        folder=Path(temporary)
        if payload.get('path'):
            path=Path(payload['path']);before=digest(path)
            progress('Проверяю единицы, срок, уровень и геометрию локального файла')
            if path.suffix.lower() in ('.grib','.grib2','.grb','.grb2'):
                with xr.open_dataset(path,engine='cfgrib',backend_kwargs={'indexpath':''}) as ds:
                    native=adapter.normalize(ds,plan).load()
            else:
                files=data_files(path,folder)
                opened=[]
                try:
                    opened=[xr.open_dataset(p) for p in files]
                    ds=xr.merge(opened,compat='no_conflicts',join='exact') if len(opened)>1 else opened[0]
                    native=adapter.normalize(ds,plan).load()
                finally:
                    for ds in opened: ds.close()
            if digest(path)!=before: raise ValueError('Файл изменился во время чтения. Повторите импорт.')
            provenance=[{'kind':'local_import','filename':path.name,'sha256':before,
                         'source_attribution':'user_selected_not_independently_verified'}]
        else:
            parts,provenance=adapter.fetch(plan,payload['credential'],folder,progress)
            native=combine_parts(parts)
        native=adapter.validate_canonical(native,plan)
        native.attrs['adapter_id']=adapter.source_id
        native.attrs['adapter_version']=adapter.adapter_version
        native.attrs['adapter_maturity']=adapter.maturity
        progress('Сохраняю проверенное поле в локальный NetCDF-кэш')
        save_native(native,folder/'field.nc')
        meta=metadata(native,identity,plan,provenance,adapter);meta['sha256']=digest(folder/'field.nc')
        atomic_json(folder/'metadata.json',meta)
        if target.exists():
            # Immutable old versions are retained, never silently overwritten.
            target.rename(cache/('.previous-'+identity+'-'+uuid.uuid4().hex[:8]))
        shutil.move(str(folder),str(target))
    return {'field':meta,'cached':False}


def run(payload,*,registry: AdapterRegistry = DEFAULT_REGISTRY):
    import xarray as xr
    folder=Path(payload['work']);folder.mkdir(parents=True,exist_ok=True)
    def progress(message): atomic_json(folder/'progress.json',{'message':message})
    action=payload['action']
    if action=='prepare': return prepare(payload,progress,registry=registry)
    if action=='render':
        from affine import Affine

        from .render import render
        g=payload['grid'];g['transform']=Affine(*g['transform'])
        with xr.open_dataset(payload['native']) as ds:
            result=render(ds,g,payload['output'],payload.get('options'))
        atomic_json(Path(payload['output'])/'render.json',result)
        return {'rendered':True}
    if action=='probe':
        from .render import sampling
        rows=[]
        for item in payload['fields']:
            with xr.open_dataset(item['path']) as ds:
                v=sampling(ds,payload['lon'],payload['lat']);value=float(v['value'])
                row={'id':item['id'],'value':value if __import__('math').isfinite(value) else None,
                     'distance_km':float(v['distance_km']) if __import__('math').isfinite(float(v['distance_km'])) else None}
                if 'u' in v:
                    import math
                    u=float(v['u']);north=float(v['v'])
                    if math.isfinite(u+north):
                        row.update(u=u,v=north)
                        if math.hypot(u,north)>=.1: row['direction_from_deg']=(math.degrees(math.atan2(-u,-north))+360)%360
                rows.append(row)
        return {'rows':rows}
    if action=='difference':
        from .render import difference
        target=Path(payload['cache'])/payload['identity']
        old=checked_cache(target)
        if old: return {'field':old,'cached':True}
        with xr.open_dataset(payload['first']['path']) as a,xr.open_dataset(payload['second']['path']) as b:
            native=difference(a,b,payload['first']['id'],payload['second']['id'])
        input_metadata=payload.get('input_metadata',[])
        first_source=input_metadata[0].get('source') if input_metadata and isinstance(input_metadata[0],dict) else None
        adapter=get_runtime_adapter(first_source,registry=registry)
        target.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.difference-',dir=target.parent) as temporary:
            tmp=Path(temporary);save_native(native,tmp/'field.nc')
            meta=metadata(native,payload['identity'],{},[{'operation':'A-B','inputs':input_metadata}],adapter)
            meta['sha256']=digest(tmp/'field.nc');atomic_json(tmp/'metadata.json',meta)
            if target.exists(): target.rename(target.with_name('.previous-'+target.name+'-'+uuid.uuid4().hex[:8]))
            shutil.move(str(tmp),str(target))
        return {'field':meta,'cached':False}
    raise ValueError('Неизвестная операция реанализа.')


def main():
    logging.disable(logging.CRITICAL)
    load_installed_adapters()
    payload=json.load(sys.stdin);folder=Path(payload['work'])
    try:
        result=run(payload);atomic_json(folder/'result.json',{'ok':True,'result':result})
    except (AccessError,ValueError) as exc:
        # Only our own validation messages are public; provider errors may contain tokens.
        tb=exc.__traceback__
        while tb and tb.tb_next: tb=tb.tb_next
        origin=Path(tb.tb_frame.f_code.co_filename).resolve() if tb else None
        own=origin is not None and origin.parent==Path(__file__).resolve().parent
        if own and (isinstance(exc,AccessError) or type(exc) is ValueError):
            message=str(exc)[:1200]
            credential=payload.get('credential',{})
            for name in ('token','password','login'):
                secret=credential.get(name)
                if secret: message=message.replace(secret,'[скрыто]')
        else: message='Источник отклонил запрос или данные не прошли проверку. Проверьте срок, область и доступ.'
        atomic_json(folder/'result.json',{'ok':False,'error':message})
    except ImportError:
        atomic_json(folder/'result.json',{'ok':False,'error':'Нет библиотеки для этого формата. Повторите install; для GRIB дополнительно установите requirements-grib.txt.'})
    except Exception:
        atomic_json(folder/'result.json',{'ok':False,'error':'Не удалось получить или обработать поле. Проверьте подключение, права на набор данных и формат файла. Непроверенный результат не сохранён.'})

if __name__=='__main__': main()
