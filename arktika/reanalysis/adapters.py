"""Получение настоящих поднаборов CDS и NASA CMR/OPeNDAP. Вызывается в дочернем процессе."""
from __future__ import annotations
import contextlib
import hashlib
from pathlib import Path
from urllib.parse import urlsplit
import zipfile
from .catalog import plan
from .normalize import normalize, select, variable

NASA_HOSTS = frozenset({'opendap.earthdata.nasa.gov', 'goldsmr4.gesdisc.eosdis.nasa.gov',
                       'goldsmr5.gesdisc.eosdis.nasa.gov', 'urs.earthdata.nasa.gov'})
CMR = 'https://cmr.earthdata.nasa.gov/search/granules.umm_json'
MAX_BYTES = 512 * 1024 * 1024


def file_digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def nasa_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname not in NASA_HOSTS or p.username or p.password or p.fragment or p.port not in (None, 443):
        raise ValueError('CMR вернул неподдерживаемый адрес сервиса NASA.')
    return url


def nasa_session(credential):
    import requests
    from requests.auth import HTTPBasicAuth

    class ScopedSession(requests.Session):
        def __init__(self):
            super().__init__()
            # In particular, do not load the server user's .netrc or persist cookies.
            self.trust_env = False
            self.max_redirects = 8

        def send(self, prepared, **kwargs):
            nasa_url(prepared.url)
            host = urlsplit(prepared.url).hostname
            prepared.headers.pop('Authorization', None)
            if credential.get('format') == 'token':
                prepared.headers['Authorization'] = 'Bearer ' + credential['token']
            elif host == 'urs.earthdata.nasa.gov':
                HTTPBasicAuth(credential['login'], credential['password'])(prepared)
            kwargs.setdefault('timeout', (15, 90))
            kwargs['verify'] = True
            return super().send(prepared, **kwargs)

        def rebuild_auth(self, prepared, response):
            # send() performs origin validation and supplies only origin-appropriate credentials.
            prepared.headers.pop('Authorization', None)

    return ScopedSession()


def find_opendap(payload):
    candidates = []
    for item in payload.get('items', []):
        umm = item.get('umm', {})
        for link in umm.get('RelatedUrls', []):
            url = link.get('URL', '')
            info = ' '.join(str(link.get(k, '')) for k in ('Description', 'Type', 'Subtype')).lower()
            if 'opendap' not in info and urlsplit(url).hostname != 'opendap.earthdata.nasa.gov':
                continue
            nasa_url(url)
            if urlsplit(url).query:
                continue
            for suffix in ('.dmr.html', '.html', '.dmr.xml', '.dmr'):
                if url.endswith(suffix):
                    url = url[:-len(suffix)]
                    break
            candidates.append((0 if 'opendap.earthdata.nasa.gov' in url else 1, url, umm.get('GranuleUR', '')))
    if not candidates:
        raise ValueError('В CMR нет поддерживаемой ссылки OPeNDAP на этот срок. Подмена файла или дня запрещена.')
    _, url, granule = sorted(set(candidates))[0]
    return url, granule


def retrieve_merra(r, credential, folder):
    import requests
    import xarray as xr
    p = plan(r)
    day = r['time'][:10]
    # No private token is sent to the public discovery API.
    with requests.Session() as discovery:
        discovery.trust_env = False
        response = discovery.get(CMR, params={'short_name': p['dataset'], 'version': '5.12.4',
                                 'temporal': day + 'T00:00:00Z,' + day + 'T23:59:59Z', 'page_size': 5}, timeout=(15, 60))
        response.raise_for_status()
        if len(response.content) > 4 * 1024 * 1024:
            raise ValueError('Слишком большой ответ каталога CMR.')
        url, granule = find_opendap(response.json())
    with nasa_session(credential) as session:
        dap = url.replace('https://', 'dap4://', 1) if urlsplit(url).hostname == 'opendap.earthdata.nasa.gov' else url
        # Xarray >=2025.4 and pydap >=3.5: select only requested variables/time/level/region before .load().
        with xr.open_dataset(dap, engine='pydap', backend_kwargs={'session': session, 'timeout': 90}) as ds:
            thin, _, _ = select(ds, r)
            selected = [variable(thin, name).name for name in p['variables']]
            thin = thin[selected].load()
            arrays, meta = normalize(thin, r)
    meta['provenance'] = dict(transport='CMR/OPeNDAP', collection=p['dataset'], collection_version='5.12.4',
                              granule=granule, source_url=url)
    return arrays, meta


def retrieve_cds(r, credential, folder):
    import cdsapi
    import requests
    p = plan(r)
    # Public schema guards publication years / variable names. It is not a proof of hourly availability.
    with requests.Session() as public:
        public.trust_env = False
        response = public.get('https://cds.climate.copernicus.eu/api/retrieve/v1/processes/' + p['dataset'], timeout=(15, 60))
        response.raise_for_status()
        if len(response.content) > 4 * 1024 * 1024:
            raise ValueError('Слишком большой ответ схемы CDS.')
        schema = response.json().get('inputs', {})
    for key in ('year', 'variable', 'level_location', 'pressure_level', 'level_type'):
        if key not in p['request']:
            continue
        prop = schema.get(key, {}).get('schema', {})
        allowed = prop.get('enum') or prop.get('items', {}).get('enum')
        selected = p['request'][key]
        selected = selected if isinstance(selected, list) else [selected]
        if allowed and any(str(v) not in allowed for v in selected):
            if key == 'year':
                raise ValueError('Этот год не опубликован в схеме CDS. Доступные годы: ' + min(allowed) + '–' + max(allowed) + '.')
            raise ValueError('Запрос не соответствует актуальной схеме CDS: ' + key + '.')
    target = Path(folder) / 'source.download'
    client = cdsapi.Client(url='https://cds.climate.copernicus.eu/api', key=credential['token'],
                           quiet=True, debug=False, timeout=60, retry_max=3)
    # Credentials exist only in the child process and are never written to .cdsapirc.
    result = client.retrieve(p['dataset'], p['request'])
    size = getattr(result, 'content_length', None)
    if size and int(size) > MAX_BYTES:
        raise ValueError('Ответ CDS больше 512 МиБ; уменьшите область.')
    result.download(str(target))
    if target.stat().st_size > MAX_BYTES:
        target.unlink()
        raise ValueError('Ответ CDS больше 512 МиБ; уменьшите область.')
    arrays, meta = read_local(target, r)
    meta['provenance'] = dict(transport='CDS', dataset=p['dataset'], query=p['request'], source_sha256=file_digest(target))
    target.unlink(missing_ok=True)
    return arrays, meta


def read_local(path, r):
    """Read a local provider file (or CDS ZIP of NetCDFs), without interpreting arbitrary TIFFs."""
    import xarray as xr
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Выберите существующий NetCDF/GRIB размером не более 512 МиБ.')
    with open(path, 'rb') as file:
        head = file.read(8)
    with contextlib.ExitStack() as stack:
        if head.startswith(b'PK'):
            import tempfile
            tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            archive = stack.enter_context(zipfile.ZipFile(path))
            infos = [i for i in archive.infolist() if i.filename.lower().endswith(('.nc', '.nc4'))]
            if not 1 <= len(infos) <= 8 or sum(i.file_size for i in infos) > MAX_BYTES:
                raise ValueError('ZIP должен содержать не более 8 NetCDF, суммарно до 512 МиБ.')
            datasets = []
            for i, info in enumerate(infos):
                out = tmp / f'{i}.nc'
                # Never trust archive member paths.
                with archive.open(info) as src, out.open('wb') as dst:
                    import shutil
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                datasets.append(stack.enter_context(xr.open_dataset(out)))
            ds = xr.merge(datasets, compat='no_conflicts', join='exact')
        elif head.startswith(b'GRIB'):
            ds = stack.enter_context(xr.open_dataset(path, engine='cfgrib', backend_kwargs={'indexpath': '', 'read_keys': ['uvRelativeToGrid']}))
        elif head.startswith(b'CDF') or head.startswith(b'\x89HDF'):
            ds = stack.enter_context(xr.open_dataset(path))
        else:
            raise ValueError('Файл не является NetCDF/GRIB. HTML-страница входа не принимается за данные.')
        return normalize(ds, r)


def retrieve(r, credential, folder):
    if r['source'] == 'merra2':
        return retrieve_merra(r, credential, folder)
    if credential.get('provider') != 'cds' or not credential.get('token'):
        raise ValueError('Нужен PAT Copernicus CDS. Реквизиты NCAR/GDEX не подходят для полевых слоёв CDS.')
    return retrieve_cds(r, credential, folder)
