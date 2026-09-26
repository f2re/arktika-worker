"""Доступ к ERA5: отдельные учётные данные, ограниченные запросы и кэш.

CDS использует PAT, GDEX — свой .netrc. Секреты не записываются на диск.
Скачивание выполняется дочерним процессом: отмена не оставляет работу в фоне.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from urllib.parse import urlsplit
from .download import atomic_json, digest
from .interpretation import utc
from .network import Cancelled

CDS_HOST = 'cds.climate.copernicus.eu'
CDS_URL = 'https://' + CDS_HOST + '/api'
GDEX_HOSTS = {'gdex.ucar.edu', 'rda.ucar.edu', 'tds.gdex.ucar.edu', 'thredds.rda.ucar.edu'}
LEVELS = [1,2,3,5,7,10,20,30,50,70,100,125,150,175,200,225,250,300,
          350,400,450,500,550,600,650,700,750,775,800,825,850,875,900,925,950,975,1000]
PL = ['temperature', 'specific_humidity', 'ozone_mass_mixing_ratio', 'geopotential']
SFC = ['skin_temperature','surface_pressure','2m_temperature','2m_dewpoint_temperature',
       '10m_u_component_of_wind','10m_v_component_of_wind','total_cloud_cover',
       'sea_ice_cover','land_sea_mask']
MAX_FILE_BYTES = 512 * 1024**2


def credentials_from_text(text, kind='auto'):
    """Парсер никогда не возвращает исходную строку в ошибке."""
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 65536:
        raise ValueError('Файл доступа пуст или превышает 64 КиБ.')
    if kind == 'token':
        token = text.strip()
        if len(token)>2048 or any(c.isspace() for c in token):
            raise ValueError('Введите персональный токен CDS, без адреса и пробелов.')
        return {'provider':'cds','token':token,'format':'token'}
    if kind not in ('auto','netrc','cdsapirc'):
        raise ValueError('Неизвестный формат доступа.')
    if kind == 'cdsapirc' or (kind == 'auto' and re.search(r'^\s*url\s*:',text,re.M)):
        fields = {}
        for line in text.splitlines():
            line=line.strip()
            if not line or line.startswith('#'): continue
            key,sep,value=line.partition(':')
            if not sep or key.strip() not in ('url','key') or key.strip() in fields:
                raise ValueError('В .cdsapirc ожидаются только url и key, по одному разу.')
            fields[key.strip()]=value.strip().strip('"\'')
        if fields.get('url','').rstrip('/') != CDS_URL:
            raise ValueError('Адрес .cdsapirc должен быть официальным API Copernicus CDS.')
        out=credentials_from_text(fields.get('key',''),'token');out['format']='cdsapirc'
        return out
    try:
        words=shlex.split(text,comments=True); hosts={}; i=0
        while i<len(words):
            if words[i] != 'machine': raise ValueError()
            host=words[i+1].lower(); i+=2
            if host in hosts: raise ValueError()
            fields={}
            while i<len(words) and words[i]!='machine':
                key=words[i]
                if key not in ('login','password','account') or key in fields: raise ValueError()
                fields[key]=words[i+1]; i+=2
            if not fields.get('login') or not fields.get('password'): raise ValueError()
            hosts[host]=fields
    except (ValueError,IndexError):
        raise ValueError('Некорректный .netrc: нужны machine, login и password; default и macdef запрещены.') from None
    relevant={h:v for h,v in hosts.items() if h in GDEX_HOSTS or h==CDS_HOST}
    if not relevant:
        raise ValueError('В .netrc нет записи CDS или NCAR/GDEX. Доступ NASA Earthdata не даёт доступ к ERA5.')
    if CDS_HOST in relevant and len(relevant)>1:
        raise ValueError('Загрузите отдельный файл для CDS или NCAR/GDEX, без смешения провайдеров.')
    if CDS_HOST in relevant:
        out=credentials_from_text(relevant[CDS_HOST]['password'],'token');out['format']='netrc'
        return out
    return {'provider':'gdex','hosts':relevant,'format':'netrc'}


def public_credentials(credential):
    return {'present':bool(credential), 'provider':credential.get('provider') if credential else None,
            'format':credential.get('format') if credential else None, 'storage':'memory'}


def channels_list(values):
    if not isinstance(values,list) or not values or len(values)>7 or any(isinstance(c,bool) or str(c) not in {str(i) for i in range(4,11)} for c in values):
        raise ValueError('Выберите ИК-каналы 4–10; RGB и видимые каналы не калибруются по температуре ERA5.')
    return sorted({int(v) for v in values})


def plan_requests(stamp, channels, area, provider='cds', now=None):
    """План — без сети и секретов. Область N/W/S/E, шаг 0,25°, два часа."""
    channels=channels_list(channels)
    if provider not in ('cds','gdex'): raise ValueError('Источник: CDS или GDEX.')
    when=utc(stamp)
    now=now or dt.datetime.now(dt.timezone.utc)
    if when < dt.datetime(1940,1,1,tzinfo=dt.timezone.utc) or when>now:
        raise ValueError('Срок ERA5 должен находиться между 1940 годом и настоящим временем.')
    if not isinstance(area,list) or len(area)!=4 or any(isinstance(v,bool) for v in area):
        raise ValueError('Область: север, запад, юг, восток.')
    try: n,w,s,e=map(float,area)
    except (TypeError,ValueError): raise ValueError('Границы области должны быть числами.') from None
    if not (-90<=s<n<=90 and -180<=w<=180 and -180<=e<=180):
        raise ValueError('Проверьте границы широты и долготы.')
    span=(e-w)%360
    if not 0<span<=60 or n-s>30 or (n-s)*span>800:
        raise ValueError('Выберите опорную область: не более 60° долготы, 30° широты и 800 квадратных градусов.')
    if not all(abs(v*4-round(v*4))<1e-7 for v in (n,w,s,e)):
        raise ValueError('Границы области задаются с шагом 0,25°.')
    lo=when.replace(minute=0,second=0,microsecond=0)
    hi=lo if when==lo else lo+dt.timedelta(hours=1)
    times=sorted({lo,hi})
    areas=[[n,w,s,e]] if e>w else [[n,w,s,180.],[n,-180.,s,e]]
    areas=[a for a in areas if a[3]>a[1]]
    requests=[]
    for domain in areas:
        for t in times:
            common={'product_type':['reanalysis'],'year':[t.strftime('%Y')],
                    'month':[t.strftime('%m')],'day':[t.strftime('%d')],'time':[t.strftime('%H:00')],
                    'area':domain,'grid':[.25,.25], 'data_format':'netcdf','download_format':'unarchived'}
            for group,variables in (('pressure',PL),('surface',SFC)):
                query=dict(common,variable=variables)
                if group=='pressure': query['pressure_level']=list(map(str,LEVELS))
                requests.append({'dataset':'reanalysis-era5-'+('pressure-levels' if group=='pressure' else 'single-levels'),
                                 'group':group,'time':t.isoformat().replace('+00:00','Z'),'request':query})
    notes=['37 изобарических уровней; это не полный набор 137 модельных уровней.',
           'Только открытая вода с малой модельной облачностью; это не независимая облачная маска.',
           'CO₂ и прочие неполучаемые газы: стандартный профиль RTTOV, отмечается в отчёте.']
    if 4 in channels: notes.append('Канал 4: только ночные опоры, высота Солнца не выше −6°.')
    delayed=(now-hi).total_seconds()<5*86400
    if delayed: notes.append('ERA5T обычно отстаёт примерно на 5 суток. Запрос текущего срока не заменяется другим днём.')
    return {'version':'era5-electrol-proxy-1','provider':provider,'time':when.isoformat().replace('+00:00','Z'),
            'channels':channels,'area':[n,w,s,e], 'times':[t.isoformat().replace('+00:00','Z') for t in times],
            'time_weight':0. if hi==lo else (when-lo).total_seconds()/3600,
            'requests':requests,'likely_not_available':delayed,'notes':notes}


def cancelled(event):
    if event and event.is_set(): raise Cancelled()


def run_worker(payload, cancel=None, timeout=7200):
    """Credentials only on stdin. No raw worker output or provider errors in logs."""
    runner=Path(__file__).resolve().parents[1]/'scripts/era5_worker.py'
    proc=subprocess.Popen([sys.executable,str(runner)],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    try:
        proc.stdin.write(json.dumps(payload).encode());proc.stdin.close()
        end=time.monotonic()+timeout
        while proc.poll() is None:
            cancelled(cancel)
            if time.monotonic()>end: raise ValueError('ERA5: превышено время ожидания. Повторный запуск использует завершённые файлы кэша.')
            time.sleep(.15)
        if proc.returncode:
            messages={11:'Доступ ERA5 отклонён. Проверьте учётные данные и принятие условий набора.',
                      12:'ERA5 не найден для этого срока. У GDEX архив может обновляться позже CDS.',
                      13:'Не установлены зависимости ERA5. См. раздел установки в справке.',
                      14:'Ответ источника ERA5 не соответствует ожидаемому NetCDF или превышает лимит.',
                      15:'GDEX не предоставляет ожидаемую службу поднабора для этого файла.'}
            raise ValueError(messages.get(proc.returncode,'Не удалось получить ERA5: сеть, очередь источника или формат ответа. Секретные подробности ответа не публикуются.'))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(3)
            except subprocess.TimeoutExpired: proc.kill();proc.wait()


def validate_netcdf(path):
    p=Path(path)
    if not p.is_file() or not 8<=p.stat().st_size<=MAX_FILE_BYTES:
        raise ValueError('ERA5: неверный размер файла.')
    with p.open('rb') as f: magic=f.read(8)
    if not (magic[:3]==b'CDF' or magic==b'\x89HDF\r\n\x1a\n'):
        raise ValueError('ERA5: вместо NetCDF получена ошибка или другой формат.')
    import xarray as xr
    with xr.open_dataset(p) as ds:
        if not ds.data_vars: raise ValueError('ERA5: файл не содержит переменных.')


def retrieve_plan(plan, credential, folder, cancel=None, progress=lambda message: None):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    files=[]
    for number,item in enumerate(plan['requests'],1):
        cancelled(cancel)
        key=hashlib.sha256(json.dumps({'provider':plan['provider'],**item},sort_keys=True).encode()).hexdigest()
        target=folder/(key+'.nc');meta=folder/(key+'.json')
        valid=False
        try:
            previous=json.loads(meta.read_text(encoding='utf-8'))
            valid=target.is_file() and previous['sha256']==digest(target,cancel)
            age=(dt.datetime.now(dt.timezone.utc)-utc(item['time'])).total_seconds()
            if age<90*86400 and time.time()-previous.get('retrieved_at',0)>86400:
                valid=False
            if valid: validate_netcdf(target)
        except (OSError,ValueError,KeyError,ImportError): valid=False
        if not valid:
            progress('ERA5: запрос {}/{} · {} · {}'.format(number,len(plan['requests']),item['group'],item['time']))
            part=folder/(key+'.part.nc')
            try:
                run_worker({'provider':plan['provider'],'credential':credential,'item':item,'target':str(part)},cancel)
                validate_netcdf(part);cancelled(cancel)
                os.replace(part,target)
                atomic_json(meta,{'sha256':digest(target,cancel),'provider':plan['provider'],'item':item,'retrieved_at':time.time()})
            finally: part.unlink(missing_ok=True)
        else: progress('ERA5: файл {}/{} из проверенного кэша'.format(number,len(plan['requests'])))
        files.append({'path':str(target),'sha256':digest(target,cancel),'group':item['group'],
                      'time':item['time'],'area':item['request']['area']})
    return files
