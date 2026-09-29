"""Контракты полей и запросов. Никаких данных или токенов в реестре."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math

VERSION = 'reanalysis-fields-1'
UTC = dt.timezone.utc
# Common levels supported by the selected collections, not every level in each archive.
LEVELS = [1000, 925, 850, 700, 500, 400, 300, 250, 200, 150, 100]
SOURCES = {
    'era5': {'name': 'ERA5', 'auth': 'cds', 'resolution_km': 28., 'start_year': 1940},
    'carra2': {'name': 'CARRA2', 'auth': 'cds', 'resolution_km': 2.5, 'start_year': 1986},
    'merra2': {'name': 'MERRA-2', 'auth': 'earthdata', 'resolution_km': 56., 'start_year': 1980},
}
# Each mapping lists the provider's actual variables. Wind is never inferred from clouds.
FIELDS = {
    't': {'name': 'Температура воздуха', 'units': '°C', 'vertical': 'pressure', 'range': [-60, 20], 'step': 5,
          'era5': ['temperature'], 'carra2': ['temperature'], 'merra2': ['T'], 'kind': 'temperature'},
    'h': {'name': 'Геопотенциальная высота', 'units': 'м', 'vertical': 'pressure', 'range': [0, 16000], 'step': 200,
          'era5': ['geopotential'], 'carra2': ['geopotential'], 'merra2': ['H'], 'kind': 'height'},
    'rh': {'name': 'Относительная влажность', 'units': '%', 'vertical': 'pressure', 'range': [0, 100], 'step': 10,
           'era5': ['relative_humidity'], 'carra2': ['relative_humidity'], 'merra2': ['RH'], 'kind': 'humidity'},
    'wind': {'name': 'Ветер', 'units': 'м/с', 'vertical': 'pressure', 'range': [0, 60], 'step': 10,
             'era5': ['u_component_of_wind', 'v_component_of_wind'], 'merra2': ['U', 'V'], 'kind': 'wind'},
    'wind_speed': {'name': 'Скорость ветра', 'units': 'м/с', 'vertical': 'pressure', 'range': [0, 60], 'step': 10,
                   'carra2': ['wind_speed'], 'kind': 'speed'},
    'mslp': {'name': 'Давление на уровне моря', 'units': 'гПа', 'vertical': 'surface', 'range': [960, 1040], 'step': 4,
             'era5': ['mean_sea_level_pressure'], 'carra2': ['mean_sea_level_pressure'], 'kind': 'pressure'},
    't2m': {'name': 'Температура на 2 м', 'units': '°C', 'vertical': 'surface', 'range': [-40, 30], 'step': 5,
            'era5': ['2m_temperature'], 'carra2': ['2m_temperature'], 'kind': 'temperature'},
    'wind10': {'name': 'Ветер на 10 м', 'units': 'м/с', 'vertical': 'surface', 'range': [0, 35], 'step': 5,
               'era5': ['10m_u_component_of_wind', '10m_v_component_of_wind'], 'kind': 'wind'},
    'wind_speed10': {'name': 'Скорость ветра на 10 м', 'units': 'м/с', 'vertical': 'surface', 'range': [0, 35], 'step': 5,
                     'carra2': ['10m_wind_speed'], 'kind': 'speed'},
    'sst': {'name': 'Температура поверхности моря', 'units': '°C', 'vertical': 'surface', 'range': [-2, 20], 'step': 2,
            'era5': ['sea_surface_temperature'], 'carra2': ['sea_surface_temperature'], 'kind': 'temperature'},
    'ice': {'name': 'Доля морского льда', 'units': '%', 'vertical': 'surface', 'range': [0, 100], 'step': 10,
            'era5': ['sea_ice_cover'], 'carra2': ['sea_ice_area_fraction'], 'kind': 'fraction'},
    'omega': {'name': 'Вертикальная скорость ω', 'units': 'Па/с', 'vertical': 'pressure', 'range': [-1, 1], 'step': .2,
              'era5': ['vertical_velocity'], 'merra2': ['OMEGA'], 'kind': 'omega'},
}
# MERRA-2 surface diagnostics are time means (00:30, ...), not instantaneous ERA5 analyses.
# Expose them with their real temporal semantics; differences with instantaneous data are rejected.
for _id, _var in [('mslp', 'SLP'), ('t2m', 'T2M'), ('wind10', ['U10M', 'V10M'])]:
    FIELDS[_id]['merra2'] = _var if isinstance(_var, list) else [_var]
for _id, _vars in [('wind_speed', ['u_component_of_wind', 'v_component_of_wind']), ('wind_speed10', ['10m_u_component_of_wind', '10m_v_component_of_wind'])]:
    FIELDS[_id]['era5'] = _vars
    FIELDS[_id]['merra2'] = ['U', 'V'] if _id == 'wind_speed' else ['U10M', 'V10M']
ALIASES = {
    'temperature': ['t', 'T', 'air_temperature'], 'geopotential': ['z', 'gh', 'geopotential'],
    'relative_humidity': ['r', 'rh', 'RH'], 'u_component_of_wind': ['u', 'U'], 'v_component_of_wind': ['v', 'V'],
    'mean_sea_level_pressure': ['msl', 'mslp', 'prmsl', 'MSL'], '2m_temperature': ['t2m', '2t', 't2', 'tas'],
    '10m_u_component_of_wind': ['u10', '10u'], '10m_v_component_of_wind': ['v10', '10v'],
    'wind_speed': ['si', 'ws', 'wind_speed'], '10m_wind_speed': ['si10', '10si', 'ws10'],
    'sea_surface_temperature': ['sst'], 'sea_ice_cover': ['siconc', 'ci'], 'sea_ice_area_fraction': ['siconc', 'ci'],
    'vertical_velocity': ['w', 'omega'],
}


def stamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('Укажите срок ISO 8601 с часовым поясом.')
    try:
        t = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Неверный срок ISO 8601.') from None
    if t.tzinfo is None:
        raise ValueError('У срока должен быть часовой пояс; интерфейс использует UTC.')
    return t.astimezone(UTC)


def iso(t):
    return t.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def number(v, title):
    if isinstance(v, bool):
        raise ValueError(title + ': нужно число.')
    try:
        n = float(v)
    except (ValueError, TypeError):
        raise ValueError(title + ': нужно число.') from None
    if not math.isfinite(n):
        raise ValueError(title + ': нужно конечное число.')
    return n


def area(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError('Область: север, запад, юг, восток.')
    n, w, s, e = [number(v, 'Граница области') for v in value]
    if not -90 <= s < n <= 90 or not -180 <= w < e <= 180:
        raise ValueError('Неверная область. Область через 180° загрузите двумя частями.')
    return [n, w, s, e]


def request(data):
    source, field = data.get('source'), data.get('field')
    if source not in SOURCES or field not in FIELDS or source not in FIELDS[field]:
        raise ValueError('Поле не поддерживается выбранным источником.')
    spec = FIELDS[field]
    t = stamp(data.get('time'))
    if t.year < SOURCES[source]['start_year'] or t > dt.datetime.now(UTC):
        raise ValueError('Срок вне доступного периода реанализа; будущие поля не запрашиваются.')
    temporal = 'mean_1h' if source == 'merra2' and spec['vertical'] == 'surface' else 'instant'
    minute = 30 if temporal == 'mean_1h' else 0
    step = 3 if source == 'carra2' or (source == 'merra2' and spec['vertical'] == 'pressure') else 1
    if t.minute != minute or t.second or t.microsecond or t.hour % step:
        suffix = 'середина часового среднего, HH:30 UTC' if minute else f'каждые {step} ч, HH:00 UTC'
        raise ValueError('Выберите исходный срок: ' + suffix + '. Соседний срок не подставляется.')
    level = None
    if spec['vertical'] == 'pressure':
        level = number(data.get('level'), 'Уровень, гПа')
        if level not in LEVELS:
            raise ValueError('Выберите один из доступных изобарических уровней.')
        level = int(level)
    elif data.get('level') not in (None, '', 'surface'):
        raise ValueError('У поверхностного поля нет изобарического уровня.')
    a = area(data.get('area', [85, -25, 60, 70]))
    return dict(source=source, field=field, time=iso(t), level=level, area=a, temporal=temporal, version=VERSION)


def identity(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()[:24]


def catalog():
    rows = []
    for source, meta in SOURCES.items():
        fields = []
        for name, f in FIELDS.items():
            if source not in f:
                continue
            fields.append(dict(id=name, name=f['name'], units=f['units'], vertical=f['vertical'],
                               levels=LEVELS if f['vertical'] == 'pressure' else [], vector=f['kind'] == 'wind',
                               hour_step=3 if source == 'carra2' or (source == 'merra2' and f['vertical'] == 'pressure') else 1,
                               minute=30 if source == 'merra2' and f['vertical'] == 'surface' else 0))
        rows.append(dict(id=source, **meta, fields=fields))
    return {'sources': rows, 'version': VERSION}


def plan(r):
    f, source, t = FIELDS[r['field']], r['source'], stamp(r['time'])
    if source == 'merra2':
        dataset = 'M2T1NXSLV' if f['vertical'] == 'surface' else 'M2I3NPASM'
        return dict(dataset=dataset, version='5.12.4', variables=f[source], transport='CMR / Cloud OPeNDAP',
                    time=r['time'], area=r['area'], level=r['level'])
    q = dict(variable=f[source], year=[f'{t.year:04d}'], month=[f'{t.month:02d}'], day=[f'{t.day:02d}'],
             time=[t.strftime('%H:%M')], area=r['area'], data_format='netcdf')
    if source == 'era5':
        dataset = 'reanalysis-era5-' + ('pressure-levels' if f['vertical'] == 'pressure' else 'single-levels')
        q.update(product_type=['reanalysis'], download_format='unarchived')
        if r['level'] is not None:
            q['pressure_level'] = [str(r['level'])]
    else:
        dataset = 'reanalysis-pan-carra'
        q.update(product_type='analysis', level_type='pressure_levels' if r['level'] is not None else 'single_levels')
        if r['level'] is not None:
            q['level_location'] = [str(r['level'])]
    return {'dataset': dataset, 'request': q, 'transport': 'CDS'}
