"""Разрешённые HTTPS-источники. Секреты не передаются CMR или хранилищу CDS."""
from __future__ import annotations
import base64
import json
from pathlib import Path
from urllib.parse import urlsplit, urlencode
import zipfile
import numpy as np
from .normalize import exact_slice, spatial_slice, normalize, save_native

CDS_HOST='cds.climate.copernicus.eu'
CDS_URL='https://'+CDS_HOST+'/api'
URS='urs.earthdata.nasa.gov'
CMR='cmr.earthdata.nasa.gov'
NASA_DATA={'goldsmr4.gesdisc.eosdis.nasa.gov','goldsmr5.gesdisc.eosdis.nasa.gov',
           'data.gesdisc.earthdata.nasa.gov','opendap.earthdata.nasa.gov'}
MAX_BYTES=1024**3

class AccessError(ValueError):
    pass


def allowed_url(url):
    try:
        p=urlsplit(url)
        port=p.port
    except ValueError: return False
    if any(ord(c)<32 for c in url): return False
    return (p.scheme=='https' and not p.username and not p.password and not p.fragment
            and port in (None,443) and (p.hostname in NASA_DATA|{CDS_HOST,URS,CMR}
            or (p.hostname or '').endswith('.ecmwf.int')))


def safe_session(credential):
    import requests
    class Session(requests.Session):
        def request(self,method,url,**kwargs):
            if not allowed_url(url): raise AccessError('Источник или перенаправление вне разрешённого списка HTTPS.')
            kwargs.setdefault('timeout',(20,120))
            return super().request(method,url,**kwargs)
        def send(self,request,**kwargs):
            if not allowed_url(request.url): raise AccessError('Неожиданное перенаправление источника.')
            host=urlsplit(request.url).hostname
            for header in ('Authorization','PRIVATE-TOKEN','X-API-Key'): request.headers.pop(header,None)
            if credential.get('provider')=='cds' and host==CDS_HOST:
                request.headers['PRIVATE-TOKEN']=credential['token']
            elif credential.get('provider')=='earthdata':
                if credential.get('format')=='token' and host in NASA_DATA|{URS}:
                    request.headers['Authorization']='Bearer '+credential['token']
                elif credential.get('format')=='netrc' and host==URS:
                    value=(credential['login']+':'+credential['password']).encode()
                    request.headers['Authorization']='Basic '+base64.b64encode(value).decode()
            kwargs.setdefault('timeout',(20,120))
            return super().send(request,**kwargs)
        def rebuild_auth(self,request,response):
            # Never consult ~/.netrc; send() assigns credentials to exact hosts.
            for header in ('Authorization','PRIVATE-TOKEN','X-API-Key'): request.headers.pop(header,None)
    session=Session();session.trust_env=False;session.max_redirects=8
    return session


def json_get(session,url,params=None,limit=8*1024**2):
    with session.get(url,params=params,stream=True) as response:
        if response.status_code in (401,403): raise AccessError('Источник отклонил доступ. Проверьте реквизиты и принятие условий набора.')
        response.raise_for_status()
        blocks=[];size=0
        for block in response.iter_content(65536):
            size+=len(block)
            if size>limit: raise AccessError('Ответ каталога превысил допустимый размер.')
            blocks.append(block)
        return json.loads(b''.join(blocks))


def validate_cds_schema(schema,request):
    """Use the actual provider enum, notably CARRA2 publication years."""
    inputs=schema.get('inputs',{})
    if not inputs: raise AccessError('CDS не вернул описание API; запрос не отправлен.')
    for key,value in request.items():
        if key not in inputs:
            # ERA5 supports download_format even when omitted by older process metadata.
            if key=='download_format' and value=='unarchived': continue
            raise AccessError('В текущем API CDS нет параметра '+key+'. Обновите адаптер.')
        item=inputs[key].get('schema',{})
        choices=item.get('enum') or item.get('items',{}).get('enum')
        vals=value if isinstance(value,list) else [value]
        if choices and any(str(x) not in [str(c) for c in choices] for x in vals):
            if key=='year': raise AccessError('Выбранный год пока отсутствует у источника. Доступные годы: '+', '.join(map(str,choices))+'.')
            raise AccessError('CDS не поддерживает выбранное значение '+key+'.')


def data_files(path,folder):
    """CDS may return ZIP despite a NetCDF request. Reject archive traversal/bombs."""
    path=Path(path)
    if not path.is_file() or path.stat().st_size>MAX_BYTES: raise AccessError('Файл отсутствует или больше 1 ГиБ.')
    with path.open('rb') as handle: magic=handle.read(8)
    if magic.startswith(b'PK'):
        results=[];total=0
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist())>16: raise AccessError('Слишком много файлов в ответе CDS.')
            for i,info in enumerate(archive.infolist()):
                if info.is_dir(): continue
                if Path(info.filename).suffix.lower() not in ('.nc','.nc4','.netcdf'): raise AccessError('Архив CDS содержит не NetCDF.')
                total+=info.file_size
                if total>MAX_BYTES or (info.compress_size and info.file_size/info.compress_size>1000): raise AccessError('Архив CDS слишком велик.')
                target=folder/('part-'+str(i)+'.nc')
                # Generated names, never archive-provided paths.
                with archive.open(info) as src,target.open('wb') as dst:
                    size=0
                    while block:=src.read(1024*1024):
                        size+=len(block)
                        if size>info.file_size or size>MAX_BYTES: raise AccessError('Некорректный размер архива.')
                        dst.write(block)
                results.append(target)
        if not results: raise AccessError('В архиве нет NetCDF.')
        return results
    if not (magic.startswith(b'CDF') or magic.startswith(b'\x89HDF\r\n\x1a\n')):
        raise AccessError('Источник вернул не NetCDF. HTML/XML-страница входа не является данными.')
    return [path]


def fetch_cds(plan,credential,folder,progress):
    import cdsapi
    import xarray as xr
    if credential.get('provider')!='cds' or not credential.get('token'):
        raise AccessError('Настройте Copernicus CDS в «Источниках данных».')
    results=[];provenance=[]
    with safe_session(credential) as session:
        client=cdsapi.Client(url=CDS_URL,key=credential['token'],session=session,
                             quiet=True,debug=False,progress=False,timeout=120,retry_max=2,sleep_max=5)
        for i,item in enumerate(plan['requests']):
            progress('Проверяю API и опубликованные годы CDS')
            schema=json_get(session,CDS_URL+'/retrieve/v1/processes/'+item['dataset'])
            validate_cds_schema(schema,item['request'])
            target=folder/('source-'+str(i)+'.nc')
            progress('CDS: ожидание и загрузка поднабора '+str(i+1)+'/'+str(len(plan['requests'])))
            def check_size(response,*args,**kwargs):
                size=response.headers.get('Content-Length')
                if size and int(size)>MAX_BYTES:
                    response.close();raise AccessError('CDS вернул файл больше 1 ГиБ. Уменьшите область.')
            session.hooks['response']=[check_size]
            client.retrieve(item['dataset'],item['request'],str(target))
            files=data_files(target,folder)
            opened=[]
            try:
                for p in files: opened.append(xr.open_dataset(p))
                # Multiple NetCDF files can contain separate wind components.
                ds=xr.merge(opened,compat='no_conflicts',join='exact') if len(opened)>1 else opened[0]
                results.append(normalize(ds,plan,item['request']['area']).load())
            finally:
                for ds in opened: ds.close()
            provenance.append({'dataset':item['dataset'],'request':item['request'],'schema_id':schema.get('id')})
    return results,provenance


def cmr_candidates(result,dataset,date):
    """Return only catalogue-advertised DAP links; do not guess stream numbers/URLs."""
    out=[]
    for entry in result.get('feed',{}).get('entry',[]):
        for link in entry.get('links',[]):
            url=link.get('href','');p=urlsplit(url)
            if link.get('inherited') or p.hostname not in NASA_DATA or not allowed_url(url): continue
            text=(url+' '+str(link.get('title',''))).lower()
            if '/opendap/' not in text and 'opendap' not in text: continue
            url=url.removesuffix('.html')
            if '.nc4' not in url and '.nc' not in url: continue
            if date.replace('-','') not in url: continue
            if dataset not in url: continue
            out.append({'url':url,'granule_id':entry.get('id'),'granule_title':entry.get('title')})
    unique={x['url']:x for x in out}
    return [unique[k] for k in sorted(unique)]


def fetch_merra(plan,credential,folder,progress):
    import xarray as xr
    if credential.get('provider')!='earthdata': raise AccessError('Настройте NASA Earthdata в «Источниках данных».')
    date=plan['time'][:10];results=[];provenance=[]
    with safe_session(credential) as session:
        for item in plan['requests']:
            progress('MERRA-2: поиск исходного файла через NASA CMR')
            params={'short_name':item['dataset'],'version':item['version'],
                    'temporal':date+'T00:00:00Z,'+date+'T23:59:59Z','page_size':100}
            entries=[]
            for page in range(1,11):
                response=json_get(session,'https://'+CMR+'/search/granules.json',dict(params,page_num=page))
                batch=response.get('feed',{}).get('entry',[])
                entries.extend(batch)
                if len(batch)<100: break
            else: raise AccessError('CMR вернул слишком много версий. Уточните запрос или импортируйте выбранный официальный файл.')
            candidates=cmr_candidates({'feed':{'entry':entries}},item['dataset'],date)
            if not candidates: raise AccessError('CMR не выдал ссылку OPeNDAP для этого срока и коллекции. Загрузка не подменена другим днём; можно импортировать официальный NetCDF.')
            # Different processing streams for one day require explicit resolution.
            basenames={urlsplit(c['url']).path.split('/')[-1] for c in candidates}
            if len(basenames)>1: raise AccessError('CMR вернул несколько версий одного срока. Импортируйте нужный файл явно; версии не смешиваются.')
            candidate=candidates[0]
            progress('MERRA-2: OPeNDAP — точный срок, уровень и область')
            with xr.open_dataset(candidate['url'],engine='pydap',session=session) as ds:
                # Select only needed arrays before remote reads.
                missing=set(item['variables'])-set(ds.data_vars)
                if missing: raise AccessError('В коллекции MERRA-2 отсутствуют переменные '+', '.join(sorted(missing)))
                selected=ds[item['variables']]
                results.append(normalize(selected,plan,item['area']).load())
            provenance.append(dict(candidate,dataset=item['dataset'],version=item['version'],variables=item['variables'],area=item['area'],transport='OPeNDAP subset'))
    return results,provenance


def combine_parts(parts):
    import xarray as xr
    if len(parts)==1: return parts[0]
    # Date-line subdomains are independent; flatten to a single native point row.
    keys=[k for k in ('value','latitude','longitude','u','v') if k in parts[0]]
    fields={k:(('y','x'),np.concatenate([p[k].values.reshape(-1) for p in parts])[None,:]) for k in keys}
    result=xr.Dataset(fields,attrs=parts[0].attrs).set_coords(['latitude','longitude'])
    for k in keys: result[k].attrs=dict(parts[0][k].attrs)
    return result.assign_coords({k:v for k,v in parts[0].coords.items() if v.ndim==0})
