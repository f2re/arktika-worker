"""Единый реестр источников данных и реквизитов доступа.

Реквизиты хранятся только в памяти процесса. CDS используется совместно ERA5/CARRA2,
Earthdata — для MERRA-2. Модуль не объявляет сетевой доступ проверенным до реального
запроса данных.
"""
from __future__ import annotations

import copy
import re
import shlex

from .era5_access import public_credentials, credentials_from_text
from .network import SECRETS

EARTHDATA_HOST = 'urs.earthdata.nasa.gov'

SOURCE_SPECS = (
    dict(
        id='gptl', kind='satellite', name='Арктика-М / GPTL',
        provider='GPTL', auth='gptl',
        description='Каталог, загрузка и локальная обработка спутниковых данных.',
        coverage='Арктика-М1 / Арктика-М2',
        docs='https://s3.gptl.ru/geoportal-public/pro-guide/v1/index.html',
        capabilities=['catalog','download','satellite_map'],
    ),
    dict(
        id='era5', kind='reanalysis', name='ERA5',
        provider='Copernicus CDS', auth='cds',
        description='Глобальный реанализ ECMWF. Доступ CDS также используется CARRA2.',
        coverage='Глобально · 0,25°',
        docs='https://cds.climate.copernicus.eu/datasets/reanalysis-era5-pressure-levels',
        capabilities=['access','download','cache','radiometric_reference'],
    ),
    dict(
        id='carra2', kind='reanalysis', name='CARRA2',
        provider='Copernicus CDS', auth='cds',
        description='Панарктический региональный реанализ второго поколения.',
        coverage='Севернее 40° с. ш. · 2,5 км',
        docs='https://cds.climate.copernicus.eu/datasets/reanalysis-pan-carra',
        capabilities=['access'],
    ),
    dict(
        id='merra2', kind='reanalysis', name='MERRA-2',
        provider='NASA GES DISC / Earthdata', auth='earthdata',
        description='Глобальный реанализ NASA GMAO для независимого сопоставления полей.',
        coverage='Глобально · 0,5° × 0,625°',
        docs='https://disc.gsfc.nasa.gov/datasets?keywords=MERRA-2',
        capabilities=['access'],
    ),
    dict(
        id='local', kind='local', name='Локальные GeoTIFF',
        provider='Файлы пользователя', auth='none',
        description='Локальные спутниковые растры без копирования исходников.',
        coverage='По геопривязке файла',
        docs='/docs/index.html',
        capabilities=['local_import','satellite_map'],
    ),
)


def earthdata_credentials_from_text(text, kind='auto'):
    if not isinstance(text, str):
        raise ValueError('Передайте токен Earthdata или текст .netrc.')
    if len(text.encode('utf-8')) > 65536 or '\x00' in text:
        raise ValueError('Файл доступа Earthdata слишком большой или повреждён.')
    stripped = text.strip()
    if not stripped:
        raise ValueError('Реквизиты Earthdata не заданы.')
    if kind not in ('auto','token','netrc'):
        raise ValueError('Формат Earthdata: token или netrc.')

    looks_netrc = bool(re.search(r'(^|\n)\s*(machine|default|macdef)\b', stripped, re.I))
    if kind == 'token' or (kind == 'auto' and not looks_netrc):
        token = stripped
        if len(token) > 8192 or any(c.isspace() or ord(c) < 32 for c in token):
            raise ValueError('Вставьте один Bearer-токен Earthdata без пробелов и переводов строк.')
        SECRETS.append(token)
        return {'provider':'earthdata','format':'token','token':token}

    try:
        tokens = shlex.split(stripped, comments=True, posix=True)
    except ValueError as exc:
        raise ValueError('Некорректный .netrc Earthdata.') from exc
    if not tokens or any(t.lower() in ('default','macdef') for t in tokens):
        raise ValueError('В .netrc нужен явный machine urs.earthdata.nasa.gov; default/macdef запрещены.')

    entries = {}
    i = 0
    while i < len(tokens):
        if tokens[i].lower() != 'machine' or i + 1 >= len(tokens):
            raise ValueError('Некорректный .netrc: ожидается machine, login и password.')
        host = tokens[i + 1].lower()
        i += 2
        fields = {}
        while i < len(tokens) and tokens[i].lower() != 'machine':
            key = tokens[i].lower()
            if key not in ('login','password','account') or i + 1 >= len(tokens) or key in fields:
                raise ValueError('Некорректный .netrc: допустимы login, password и account.')
            fields[key] = tokens[i + 1]
            i += 2
        if host in entries:
            raise ValueError('В .netrc повторяется machine '+host+'.')
        entries[host] = fields

    fields = entries.get(EARTHDATA_HOST)
    if not fields or not fields.get('login') or not fields.get('password'):
        raise ValueError('В .netrc нет machine urs.earthdata.nasa.gov с login и password.')
    if set(entries) - {EARTHDATA_HOST}:
        raise ValueError('Загрузите отдельный .netrc только для Earthdata, без других machine.')
    if len(fields['login']) > 512 or len(fields['password']) > 4096:
        raise ValueError('Слишком длинные реквизиты Earthdata.')
    SECRETS.append(fields['password'])
    return {'provider':'earthdata','format':'netrc','login':fields['login'],'password':fields['password']}


def public_earthdata(credential):
    return {
        'present': bool(credential),
        'provider': 'earthdata' if credential else None,
        'format': credential.get('format') if credential else None,
        'storage': 'memory',
    }


class SourceManagerMixin:
    def _sources_init(self):
        self._era5_init()
        with self.lock:
            if not hasattr(self, '_earthdata_credential'):
                self._earthdata_credential = {}

    def source_credential(self, source):
        self._sources_init()
        with self.lock:
            if source == 'merra2': return copy.deepcopy(self._earthdata_credential)
            if source not in ('era5','carra2'): raise ValueError('Неизвестный источник реанализа.')
            # GDEX is not CDS. Keep the independently configured shared CDS credential.
            if self._era5_credential.get('provider') == 'cds':
                self._source_cds_credential = copy.deepcopy(self._era5_credential)
            return copy.deepcopy(getattr(self, '_source_cds_credential', {}))

    def source_state(self):
        self._sources_init()
        gptl = self.client.token_info()
        with self.lock:
            cds = public_credentials(self.source_credential('era5'))
            earthdata = public_earthdata(self._earthdata_credential)

        access = {
            'gptl': {'present': bool(gptl.get('present')), 'storage':'memory'},
            'cds': cds,
            'earthdata': earthdata,
            'none': {'present': True, 'storage':'local'},
        }
        rows = []
        for spec in SOURCE_SPECS:
            row = dict(spec)
            state = access[spec['auth']]
            row['access'] = state
            row['status'] = 'ready' if spec['auth'] == 'none' else ('credentials_present' if state['present'] else 'credentials_missing')
            if spec['id'] in ('era5','carra2','merra2'):
                row['layer_status'] = 'fields_available'
                row['capabilities'] = list(spec['capabilities']) + ['field_download','field_map','comparison']
            else:
                row['layer_status'] = 'operational'
            rows.append(row)
        return {
            'sources': rows,
            'access_groups': {
                'cds': {'shared_by':['era5','carra2'], **cds},
                'earthdata': {'shared_by':['merra2'], **earthdata},
            },
        }

    def source_credentials(self, data):
        provider = str(data.get('provider','')).lower()
        if provider == 'cds':
            payload = {'clear': True} if data.get('clear') is True else {
                'text': data.get('text',''),
                'format': data.get('format','auto'),
            }
            parsed = {} if payload.get('clear') else credentials_from_text(payload['text'],payload['format'])
            if parsed and parsed.get('provider') != 'cds':
                raise ValueError('Это реквизиты GDEX, а не CDS. Для полей ERA5/CARRA2 нужен токен CDS.')
            self.era5_credentials(payload)
            with self.lock: self._source_cds_credential = parsed
            return self.source_state()
        if provider != 'earthdata':
            raise ValueError('Источник доступа должен быть cds или earthdata.')

        self._sources_init()
        with self.lock:
            if self.busy:
                raise ValueError('Дождитесь завершения операции перед изменением доступа.')
            self._earthdata_credential = {} if data.get('clear') is True else earthdata_credentials_from_text(
                data.get('text',''), data.get('format','auto')
            )
        return self.source_state()
