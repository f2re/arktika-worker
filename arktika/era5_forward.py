"""Спектральный аналог «Электро-Л» №2 и адаптер RTTOV 13.2.

Ни спектральная функция, ни rtcoef не содержат универсального DN→K для Арктики.
Нет подмены отсутствующего RTTOV приближённой температурой поверхности.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import urllib.request
from urllib.parse import urlsplit
import numpy as np
from .download import atomic_json, digest
from .era5_access import cancelled

COEF_NAME='rtcoef_electro-l_2_msugs_o3co2.dat'
COEF_ARCHIVE='https://nwp-saf.eumetsat.int/downloads/rtcoef_rttov14/rttov13pred54L/rtcoef_visir_rttov13pred54L_o3co2.tar.bz2'
SRF_PATH=Path(__file__).with_name('electrol2_srf.json')


def spectral_passport():
    data=json.loads(SRF_PATH.read_text(encoding='utf-8'))
    data['sha256']=digest(SRF_PATH)
    data['coefficient_file']=COEF_NAME
    data['coefficient_source']=COEF_ARCHIVE
    data['engine_contract']='pyrttov-13.2-clear-sky'
    return data


def coefficient_path(root):
    # Разрешён локальный путь администратора, но не произвольный URL из HTTP API.
    configured=os.environ.get('ARKTIKA_ELECTROL_COEF')
    return Path(configured).expanduser().resolve() if configured else Path(root)/'era5'/COEF_NAME


def coefficient_info(root):
    p=coefficient_path(root)
    if not p.is_file(): return {'present':False,'name':COEF_NAME}
    if p.name!=COEF_NAME or not 1024<=p.stat().st_size<=128*1024**2:
        return {'present':False,'name':COEF_NAME,'error':'Неверный файл коэффициентов Электро-Л №2.'}
    with p.open('rb') as f: header=f.read(4096)
    if b'<html' in header.lower() or b'<?xml' in header.lower() or b'\0' in header:
        return {'present':False,'name':COEF_NAME,'error':'Файл rtcoef не является текстовой таблицей.'}
    sha=digest(p);source=None;origin='local_file_unverified'
    try:
        meta=json.loads(p.with_suffix('.source.json').read_text(encoding='utf-8'))
        if meta.get('sha256')==sha and meta.get('source')==COEF_ARCHIVE:
            source=COEF_ARCHIVE;origin='official_https_download'
    except (OSError,ValueError): pass
    return {'present':True,'name':COEF_NAME,'sha256':sha,'source':source,'origin':origin}


def readiness(root):
    missing=[]
    for package in ('xarray','netCDF4','cdsapi'):
        if importlib.util.find_spec(package) is None: missing.append(package)
    pyrttov=importlib.util.find_spec('pyrttov') is not None
    coefficient=coefficient_info(root)
    return {'data_dependencies_missing':missing,'pyrttov':pyrttov,'coefficient':coefficient,
            'ready':pyrttov and coefficient['present'] and not missing,
            'contract':'RTTOV 13.2, Python-обёртка; не RTTOV 14',
            'spectral_proxy':'МСУ-ГС Электро-Л №2 вместо МСУ-ГС/А Арктики-М'}


def download_coefficients(root,cancel=None,progress=lambda message:None):
    """Из официального архива извлекается один обычный файл, не пути архива."""
    info=coefficient_info(root)
    if info['present']: return info
    if os.environ.get('ARKTIKA_ELECTROL_COEF'):
        raise ValueError('Исправьте путь ARKTIKA_ELECTROL_COEF; он задаётся администратором.')
    target=coefficient_path(root);target.parent.mkdir(parents=True,exist_ok=True)
    part=target.with_suffix('.part')
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            u=urlsplit(newurl)
            if u.scheme!='https' or u.hostname!='nwp-saf.eumetsat.int' or u.port not in (None,443):
                raise ValueError('Неожиданное перенаправление источника RTTOV.')
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),Redirect())
    progress('Загружаю официальный архив коэффициентов RTTOV; извлекается только Электро-Л №2.')
    class Limited:
        def __init__(self,stream): self.stream=stream;self.size=0
        def read(self,n=-1):
            cancelled(cancel);b=self.stream.read(65536 if n<0 else min(n,65536));self.size+=len(b)
            if self.size>1024**3: raise ValueError('Архив RTTOV превысил лимит 1 ГиБ.')
            return b
    try:
        with opener.open(COEF_ARCHIVE,timeout=60) as response:
            with tarfile.open(fileobj=Limited(response),mode='r|bz2') as archive:
                total=0
                for item in archive:
                    cancelled(cancel);total+=item.size
                    if item.size>256*1024**2 or total>2*1024**3:
                        raise ValueError('Недопустимый объём распакованного архива RTTOV.')
                    if Path(item.name).name!=COEF_NAME: continue
                    if not item.isfile() or not 1024<=item.size<=128*1024**2:
                        raise ValueError('Неверная запись коэффициентов в архиве.')
                    src=archive.extractfile(item)
                    with part.open('wb') as dst:
                        while True:
                            cancelled(cancel);chunk=src.read(65536)
                            if not chunk: break
                            dst.write(chunk)
                        dst.flush();os.fsync(dst.fileno())
                    if part.stat().st_size!=item.size: raise ValueError('Архив коэффициентов обрезан.')
                    os.replace(part,target)
                    info=coefficient_info(root)
                    if not info['present']: raise ValueError(info.get('error','Коэффициенты не распознаны.'))
                    info.update(source=COEF_ARCHIVE,origin='official_https_download')
                    atomic_json(target.with_suffix('.source.json'),info)
                    return info
        raise ValueError('В архиве NWP SAF нет ожидаемого файла Электро-Л №2.')
    finally: part.unlink(missing_ok=True)


def simulate_profiles(profiles,channels,coef):
    """Настоящий вызов pyrttov; отдельные группы по числу уровней над поверхностью."""
    import pyrttov
    if not profiles or any(c not in range(4,11) for c in channels): raise ValueError('Нет допустимых ИК-профилей.')
    result=np.empty((len(profiles),len(channels)),float)
    model=pyrttov.Rttov();model.FileCoef=str(coef)
    model.Options.AddInterp=True;model.Options.OzoneData=True
    model.Options.CO2Data=False;model.Options.AddSolar=False
    model.Options.DoCheckinput=True;model.Options.Verbose=False
    model.loadInst(channels=channels)
    # RTTOV renumbers loaded channels internally; runDirect() uses all loaded bands.
    try:
        for count in sorted({len(p['pressure_hpa']) for p in profiles}):
            idx=[i for i,p in enumerate(profiles) if len(p['pressure_hpa'])==count]
            rows=[profiles[i] for i in idx];n=len(rows)
            pr=pyrttov.Profiles(n,count);pr.GasUnits=1  # kg/kg over moist air
            pr.P=np.asarray([p['pressure_hpa'] for p in rows],float)
            pr.T=np.asarray([p['temperature_k'] for p in rows],float)
            pr.Q=np.asarray([p['humidity_kg_kg'] for p in rows],float)
            pr.O3=np.asarray([p['ozone_kg_kg'] for p in rows],float)
            pr.Angles=np.asarray([[p['zenith_deg'],0.,p['sun_zenith_deg'],0.] for p in rows],float)
            pr.SurfGeom=np.asarray([[p['lat'],p['lon'],0.] for p in rows],float)
            pr.SurfType=np.asarray([[1,0]]*n,np.int32) # sea / fresh water flag unused at sea
            pr.Skin=np.asarray([[p['skin_k'],35.,0.,0.,3.,5.,15.,.1,.3] for p in rows],float)
            pr.S2m=np.asarray([[p['sp_pa']/100,p['t2m_k'],p['q2m_kg_kg'],p['u10'],p['v10'],100000.] for p in rows],float)
            pr.DateTimes=np.asarray([p['datetime'] for p in rows],np.int32)
            model.Profiles=pr
            model.SurfEmisRefl=np.full((5,n,len(channels)),-1.,float) # built-in sea IR emissivity
            model.runDirect()
            bt=np.asarray(model.BtRefl,float)
            if bt.shape!=(n,len(channels)) or not np.isfinite(bt).all() or np.any((bt<120)|(bt>400)):
                raise ValueError('RTTOV вернул недопустимую яркостную температуру.')
            result[idx]=bt
    finally:
        if hasattr(model,'dropInst'): model.dropInst()
    return result


def forward_isolated(profiles,channels,root,workdir,cancel=None):
    """Fortran/RTTOV не прерывается потоком Python: процесс с ограниченным временем."""
    workdir=Path(workdir);inp=workdir/'forward-input.json';out=workdir/'forward-output.json'
    atomic_json(inp,{'profiles':profiles,'channels':channels,'coefficient':str(coefficient_path(root))})
    script=Path(__file__).resolve().parents[1]/'scripts/rttov_forward.py'
    proc=subprocess.Popen([sys.executable,str(script),str(inp),str(out)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        end=time.monotonic()+600
        while proc.poll() is None:
            cancelled(cancel)
            if time.monotonic()>end: raise ValueError('RTTOV: превышено время расчёта (10 минут).')
            time.sleep(.1)
        if proc.returncode or not out.is_file():
            raise ValueError('RTTOV не выполнил расчёт. Проверьте установку 13.2, файл коэффициентов и профиль; замены температурой ERA5 не выполняется.')
        return np.asarray(json.loads(out.read_text(encoding='utf-8'))['brightness_temperature_k'],float)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(3)
            except subprocess.TimeoutExpired: proc.kill();proc.wait()
        inp.unlink(missing_ok=True);out.unlink(missing_ok=True)
