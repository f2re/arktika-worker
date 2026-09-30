"""Изолированное выполнение: реквизиты только из stdin, результаты без секретов."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from ..download import atomic_json, digest
from .catalog import VARIABLES, SOURCES, VERSION
from .normalize import normalize, save_native
from .transport import fetch_cds, fetch_merra, combine_parts, data_files, AccessError


def metadata(native,identity,plan,provenance):
    return {'id':identity,'source':native.attrs['source'],'variable':native.attrs['variable'],
            'title':VARIABLES[native.attrs['variable']].title,'unit':native.attrs['unit'],
            'time':native.attrs['time'],'level':json.loads(native.attrs['level']),
            'time_kind':native.attrs['time_kind'],'resolution_km':native.attrs['resolution_km'],
            'area':json.loads(native.attrs['area']),'version':VERSION,
            'shape':list(native.value.shape),'wind':bool('u' in native),
            'provenance':provenance,'request':plan,
            'difference_of':json.loads(native.attrs.get('difference_of','null')),
            'source_label':native.attrs.get('source_label',SOURCES.get(native.attrs['source'],{}).get('name',native.attrs['source'])),
            'created_at':dt.datetime.now(dt.timezone.utc).isoformat()}


def checked_cache(folder):
    try:
        meta=json.loads((folder/'metadata.json').read_text(encoding='utf-8'))
        if digest(folder/'field.nc')==meta['sha256']: return meta
    except (OSError,ValueError,KeyError): pass
    return None


def prepare(payload,progress):
    import xarray as xr
    plan=payload['plan'];cache=Path(payload['cache']);identity=payload['identity'];target=cache/identity
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
                    native=normalize(ds,plan).load()
            else:
                files=data_files(path,folder)
                opened=[]
                try:
                    opened=[xr.open_dataset(p) for p in files]
                    ds=xr.merge(opened,compat='no_conflicts',join='exact') if len(opened)>1 else opened[0]
                    native=normalize(ds,plan).load()
                finally:
                    for ds in opened: ds.close()
            if digest(path)!=before: raise ValueError('Файл изменился во время чтения. Повторите импорт.')
            provenance=[{'kind':'local_import','filename':path.name,'sha256':before,
                         'source_attribution':'user_selected_not_independently_verified'}]
        else:
            fetch=fetch_merra if plan['source']=='merra2' else fetch_cds
            parts,provenance=fetch(plan,payload['credential'],folder,progress)
            native=combine_parts(parts)
        progress('Сохраняю проверенное поле в локальный NetCDF-кэш')
        save_native(native,folder/'field.nc')
        meta=metadata(native,identity,plan,provenance);meta['sha256']=digest(folder/'field.nc')
        atomic_json(folder/'metadata.json',meta)
        if target.exists():
            # Immutable old versions are retained, never silently overwritten.
            target.rename(cache/('.previous-'+identity+'-'+uuid.uuid4().hex[:8]))
        shutil.move(str(folder),str(target))
    return {'field':meta,'cached':False}


def run(payload):
    import xarray as xr
    folder=Path(payload['work']);folder.mkdir(parents=True,exist_ok=True)
    def progress(message): atomic_json(folder/'progress.json',{'message':message})
    action=payload['action']
    if action=='prepare': return prepare(payload,progress)
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
        target.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.difference-',dir=target.parent) as temporary:
            tmp=Path(temporary);save_native(native,tmp/'field.nc')
            meta=metadata(native,payload['identity'],{},[{'operation':'A-B','inputs':payload['input_metadata']}])
            meta['sha256']=digest(tmp/'field.nc');atomic_json(tmp/'metadata.json',meta)
            if target.exists(): target.rename(target.with_name('.previous-'+target.name+'-'+uuid.uuid4().hex[:8]))
            shutil.move(str(tmp),str(target))
        return {'field':meta,'cached':False}
    raise ValueError('Неизвестная операция реанализа.')


def main():
    logging.disable(logging.CRITICAL)
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
