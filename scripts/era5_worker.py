#!/usr/bin/env python3
"""Изолированный транспорт ERA5. Вход JSON — только stdin; секреты не выводятся."""
from __future__ import annotations
import calendar
import datetime as dt
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urljoin, urlsplit, urlencode
import xml.etree.ElementTree as ET

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from arktika.era5_access import CDS_URL, CDS_HOST, GDEX_HOSTS, MAX_FILE_BYTES, validate_netcdf


class TransferError(Exception):
    def __init__(self, code): self.code=code


def allowed_url(url):
    p=urlsplit(url); h=p.hostname or ''
    return (p.scheme=='https' and not p.username and not p.password and p.port in (None,443)
            and (h in GDEX_HOSTS or h==CDS_HOST or h.endswith('.ecmwf.int') or h=='ecmwf.int'))


def safe_session(credential):
    import requests
    class Session(requests.Session):
        def request(self,method,url,**kwargs):
            if not allowed_url(url): raise TransferError(14)
            return super().request(method,url,**kwargs)
        def send(self,request,**kwargs):
            if not allowed_url(request.url): raise TransferError(14)
            # Приватные заголовки не переходят на хранилище результатов CDS.
            if urlsplit(request.url).hostname != CDS_HOST:
                for name in (('Authorization','PRIVATE-TOKEN','X-API-Key') if credential.get('provider')=='cds' else ('PRIVATE-TOKEN','X-API-Key')):
                    request.headers.pop(name,None)
            return super().send(request,**kwargs)
        def rebuild_auth(self,prepared_request,response):
            super().rebuild_auth(prepared_request,response)
            if urlsplit(prepared_request.url).hostname != urlsplit(response.request.url).hostname:
                for name in ('Authorization','PRIVATE-TOKEN','X-API-Key'):
                    prepared_request.headers.pop(name,None)
    session=Session();session.trust_env=False
    return session


def request_bytes(session,url,credential,limit=4*1024**2):
    """GDEX: сначала публичный запрос; пароль — только точному host из netrc."""
    r=session.get(url,timeout=(20,90),stream=True)
    if r.status_code==401:
        auth_url=r.url
        fields=credential.get('hosts',{}).get(urlsplit(auth_url).hostname)
        r.close()
        if not fields: raise TransferError(11)
        r=session.get(auth_url,timeout=(20,90),stream=True,auth=(fields['login'],fields['password']))
    with r:
        if r.status_code in (401,403): raise TransferError(11)
        if r.status_code==404: raise TransferError(12)
        r.raise_for_status(); chunks=[]; size=0
        for chunk in r.iter_content(1024*256):
            size+=len(chunk)
            if size>limit: raise TransferError(14)
            chunks.append(chunk)
        return b''.join(chunks)


PARAMS={'temperature':('130','t'),'specific_humidity':('133','q'),
        'ozone_mass_mixing_ratio':('203','o3'),'geopotential':('129','z'),
        'skin_temperature':('235','skt'),'surface_pressure':('134','sp'),
        '2m_temperature':('167','t2m'),'2m_dewpoint_temperature':('168','d2m'),
        '10m_u_component_of_wind':('165','u10'),'10m_v_component_of_wind':('166','v10'),
        'total_cloud_cover':('164','tcc'),'sea_ice_cover':('031','siconc'),
        'land_sea_mask':('172','lsm')}


def gdex_catalog_selection(xml,code,hour,invariant=False):
    if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper(): raise TransferError(14)
    root=ET.fromstring(xml)
    services=[node.get('base') for node in root.iter() if node.tag.split('}')[-1]=='service'
              and (node.get('serviceType') or '').lower()=='netcdfsubset']
    if not services: raise TransferError(15)
    candidates=[]
    for node in root.iter():
        path=node.get('urlPath','')
        if '.128_'+code+'_' not in path or not path.endswith('.nc'): continue
        if not path.startswith('files/g/d633000/') or '..' in path.split('/'): raise TransferError(14)
        times=re.search(r'\.(\d{10})_(\d{10})\.nc$',path)
        if invariant or (times and times[1]<=hour<=times[2]): candidates.append(path)
    if len(candidates)!=1: raise TransferError(12)
    return services[0],candidates[0]


def gdex_download(item,target,credential):
    import xarray as xr
    import numpy as np
    session=safe_session(credential);parts=[]
    when=dt.datetime.fromisoformat(item['time'].replace('Z','+00:00'));hour=when.strftime('%Y%m%d%H')
    base='https://tds.gdex.ucar.edu'; cache={}
    try:
        with tempfile.TemporaryDirectory(prefix='era5-gdex-') as tmp:
            for variable in item['request']['variable']:
                code,name=PARAMS[variable];inv=name=='lsm'
                collection='e5.oper.invariant' if inv else 'e5.oper.an.'+('pl' if item['group']=='pressure' else 'sfc')
                suffix='' if inv else when.strftime('%Y%m')+'/'
                url=base+'/thredds/catalog/files/g/d633000/'+collection+'/'+suffix+'catalog.xml'
                if url not in cache: cache[url]=request_bytes(session,url,credential)
                service,path=gdex_catalog_selection(cache[url],code,hour,inv)
                n,w,s,e=item['request']['area']
                params={'var':'all','north':n,'west':w,'south':s,'east':e,
                        'accept':'netcdf4','addLatLon':'true'}
                if not inv: params['time']=item['time']
                endpoint=urljoin(base,service)+path+'?'+urlencode(params)
                content=request_bytes(session,endpoint,credential,MAX_FILE_BYTES)
                p=Path(tmp)/(name+'.nc');p.write_bytes(content);validate_netcdf(p)
                with xr.open_dataset(p) as ds:
                    # В параметрическом файле выбираем фактическую переменную, не имя координаты.
                    candidates=[k for k,v in ds.data_vars.items()
                                if set(v.dims)&{'lat','latitude'} and set(v.dims)&{'lon','longitude'}]
                    if len(candidates)!=1: raise TransferError(14)
                    piece=ds[[candidates[0]]].rename({candidates[0]:name}).load()
                    if inv:
                        for key in ('time','valid_time'):
                            if key in piece.dims:
                                if piece.sizes[key]!=1: raise TransferError(14)
                                piece=piece.isel({key:0},drop=True)
                        piece=piece.expand_dims(time=[np.datetime64(when.replace(tzinfo=None))])
                    parts.append(piece)
            xr.merge(parts,compat='no_conflicts',join='exact').to_netcdf(target)
    finally:
        session.close()
        for part in parts: part.close()


def cds_download(item,target,credential):
    import cdsapi
    if credential.get('provider')!='cds' or not credential.get('token'): raise TransferError(11)
    session=safe_session(credential)
    try:
        client=cdsapi.Client(url=CDS_URL,key=credential['token'],session=session,
                             quiet=True,debug=False,progress=False,timeout=60,retry_max=2)
        client.retrieve(item['dataset'],item['request'],str(target))
    except Exception as exc:
        response=getattr(exc,'response',None);code=getattr(response,'status_code',None)
        if code in (401,403): raise TransferError(11) from None
        if code==404: raise TransferError(12) from None
        raise
    finally: session.close()


def main():
    logging.disable(logging.CRITICAL)
    payload=json.loads(sys.stdin.buffer.read(131072))
    target=Path(payload['target'])
    # Не читать ~/.netrc/.cdsapirc оператора или случайные системные настройки.
    with tempfile.TemporaryDirectory(prefix='era5-home-') as home:
        os.environ['HOME']=home;os.environ['USERPROFILE']=home
        for key in ('CDSAPI_URL','CDSAPI_KEY','CDSAPI_RC','NETRC'): os.environ.pop(key,None)
        try:
            if payload['provider']=='cds': cds_download(payload['item'],target,payload['credential'])
            elif payload['provider']=='gdex': gdex_download(payload['item'],target,payload['credential'])
            else: raise TransferError(14)
            validate_netcdf(target)
        except ImportError: return 13
        except TransferError as exc: return exc.code
        except Exception: return 10
    return 0

if __name__=='__main__':
    try: sys.exit(main())
    except Exception: sys.exit(10)
