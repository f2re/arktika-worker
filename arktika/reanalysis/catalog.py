"""Каталог скалярных полей и планы запросов. Никаких сетевых побочных эффектов.

CARRA2: контракт /api/retrieve/v1/processes/reanalysis-pan-carra,
проверен 2026-09-29: level_location, не pressure_level; product_type=analysis.
MERRA-2: только instantaneous ASM, не усреднённые tavg-коллекции.
"""
from __future__ import annotations
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import math

VERSION = 'reanalysis-fields-1'
SOURCES = {
    'era5': {'name': 'ERA5', 'auth': 'cds', 'start': 1940, 'step_hours': 1,
             'resolution_km': 31., 'url': 'https://cds.climate.copernicus.eu/datasets/reanalysis-era5-pressure-levels'},
    'carra2': {'name': 'CARRA2', 'auth': 'cds', 'start': 1986, 'step_hours': 3,
              'resolution_km': 2.5, 'url': 'https://cds.climate.copernicus.eu/datasets/reanalysis-pan-carra'},
    'merra2': {'name': 'MERRA-2', 'auth': 'earthdata', 'start': 1980, 'step_hours': 3,
              'resolution_km': 55.6, 'url': 'https://www.earthdata.nasa.gov/data/catalog/ges-disc-m2i3npasm-5.12.4'},
}
LEVELS = {
    'era5': [1000,975,950,925,900,875,850,825,800,775,750,700,650,600,550,500,450,400,350,300,250,225,200,175,150,125,100,70,50,30,20,10,7,5,3,2,1],
    'carra2': [1000,950,925,900,875,850,825,800,750,700,600,500,400,300,250,200,150,100,50,30,10],
    'merra2': [1000,975,950,925,900,875,850,825,800,775,750,725,700,650,600,550,500,450,400,350,300,250,200,150,100,70,50,40,30,20,10,7,5,4,3,2,1,.7,.5,.4,.3,.1],
}

@dataclass(frozen=True)
class Variable:
    title: str
    unit: str
    vertical: str
    palette: str
    limits: tuple[float, float]
    cds: str
    carra: str | None
    merra: str | None
    aliases: tuple[str, ...]

VARIABLES = {
    't': Variable('Температура воздуха','°C','pressure','temperature',(-45,15),'temperature','temperature','T',('t','temperature','air_temperature')),
    'q': Variable('Удельная влажность','г/кг','pressure','humidity',(0,12),'specific_humidity','specific_humidity','QV',('q','specific_humidity','qv')),
    'rh': Variable('Относительная влажность','%','pressure','humidity',(0,100),'relative_humidity','relative_humidity',None,('r','rh','relative_humidity')),
    'z': Variable('Геопотенциальная высота','м','pressure','height',(0,6000),'geopotential','geopotential','H',('z','geopotential','gh','h','geopotential_height')),
    'wind': Variable('Скорость и направление ветра','м/с','pressure','wind',(0,50),'u_component_of_wind','wind_speed','U',('u','u_component_of_wind','eastward_wind','wind_speed','ws','ff')),
    'omega': Variable('Вертикальная скорость ω','Па/с','pressure','diverging',(-2,2),'vertical_velocity',None,'OMEGA',('w','omega','vertical_velocity','lagrangian_tendency_of_air_pressure')),
    'mslp': Variable('Давление на уровне моря','гПа','surface','pressure',(970,1030),'mean_sea_level_pressure','mean_sea_level_pressure','SLP',('msl','mslp','slp','prmsl','mean_sea_level_pressure','air_pressure_at_mean_sea_level')),
    't2m': Variable('Температура на 2 м','°C','surface','temperature',(-40,20),'2m_temperature','2m_temperature','T2M',('t2m','2t','tas','2m_temperature')),
    'wind10': Variable('Ветер на 10 м','м/с','surface','wind',(0,30),'10m_u_component_of_wind','10m_wind_speed','U10M',('u10','10u','u10m','10m_u_component_of_wind','10si','si10','10m_wind_speed')),
    'sst': Variable('Температура поверхности моря','°C','surface','temperature',(-2,20),'sea_surface_temperature','sea_surface_temperature',None,('sst','sea_surface_temperature')),
    'ice': Variable('Концентрация морского льда','%','surface','humidity',(0,100),'sea_ice_cover','sea_ice_area_fraction',None,('siconc','ci','sea_ice_cover','sea_ice_area_fraction')),
}


def supports(source: str, variable: str) -> bool:
    v = VARIABLES[variable]
    return source == 'era5' or (source == 'carra2' and v.carra is not None) or (source == 'merra2' and v.merra is not None)


def catalogue() -> dict:
    rows = []
    for source, spec in SOURCES.items():
        fields = []
        for key, v in VARIABLES.items():
            if supports(source, key):
                fields.append(dict(id=key,title=v.title,unit=v.unit,vertical=v.vertical,
                                   levels=LEVELS[source] if v.vertical=='pressure' else [],
                                   styles=['fill','contours','fill_contours'] if not key.startswith('wind') else ['fill','arrows','fill_arrows']))
        rows.append(dict(id=source,**spec,variables=fields))
    return {'sources':rows,'version':VERSION,'time_policy':'exact_analysis_only'}


def stamp(value: str) -> dt.datetime:
    try:
        t = dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
    except (TypeError, ValueError):
        raise ValueError('Укажите срок в ISO 8601 с часовым поясом.') from None
    if t.tzinfo is None:
        raise ValueError('У срока должен быть часовой пояс; интерфейс использует UTC.')
    t = t.astimezone(dt.timezone.utc)
    if t.minute or t.second or t.microsecond:
        raise ValueError('Нужен точный час анализа. Округление времени не выполняется.')
    return t


def validate_area(value) -> list[float]:
    if not isinstance(value,(list,tuple)) or len(value)!=4 or any(isinstance(x,bool) for x in value):
        raise ValueError('Область: [север, запад, юг, восток].')
    try:
        n,w,s,e = map(float,value)
    except (TypeError,ValueError):
        raise ValueError('Границы области должны быть числами.') from None
    if not all(math.isfinite(v) for v in (n,w,s,e)) or not -90<=s<n<=90 or not -180<=w<=180 or not -180<=e<=180 or w==e:
        raise ValueError('Проверьте широты и долготы области; запад может быть больше востока при пересечении 180°.')
    return [n,w,s,e]


def request_plan(data: dict) -> dict:
    source = str(data.get('source',''))
    key = str(data.get('variable',''))
    if source not in SOURCES or key not in VARIABLES or not supports(source,key):
        raise ValueError('Такого поля нет в каталоге выбранного источника.')
    t = stamp(data.get('time',''))
    if t.year < SOURCES[source]['start'] or t > dt.datetime.now(dt.timezone.utc):
        raise ValueError('Дата вне возможного периода реанализа; прогнозы здесь не запрашиваются.')
    step = 1 if source=='merra2' and VARIABLES[key].vertical=='surface' else SOURCES[source]['step_hours']
    if t.hour % step:
        raise ValueError(f'{SOURCES[source]["name"]}: анализ доступен с шагом {step} ч. Выберите точный срок, например 00, 03, 06 UTC.')
    level = None
    if VARIABLES[key].vertical=='pressure':
        raw = data.get('level')
        if isinstance(raw,bool): raise ValueError('Уровень давления должен быть числом.')
        try: level = float(raw)
        except (TypeError,ValueError): raise ValueError('Выберите изобарический уровень.') from None
        if level not in LEVELS[source]: raise ValueError('Уровень отсутствует в каталоге источника.')
    elif data.get('level') not in (None,''):
        raise ValueError('Для поверхностного поля изобарический уровень не задаётся.')
    area = validate_area(data.get('area',[82,-20,60,70]))
    if source=='carra2' and area[2]<40:
        raise ValueError('CARRA2 не глобальна. Южная граница области должна быть не ниже 40° с. ш.; фактическую маску задаёт файл.')
    n,w,s,e = area
    domains = [area] if w<e else [[n,w,s,180.],[n,-180.,s,e]]
    base = {'year':[t.strftime('%Y')],'month':[t.strftime('%m')],'day':[t.strftime('%d')],'time':[t.strftime('%H:%M')]}
    v = VARIABLES[key]
    tasks = []
    for domain in domains:
        if domain[1]==domain[3]: continue
        if source=='era5':
            names = [v.cds]
            if key=='wind': names += ['v_component_of_wind']
            if key=='wind10': names += ['10m_v_component_of_wind']
            req = dict(base,product_type=['reanalysis'],variable=names,area=domain,data_format='netcdf',download_format='unarchived')
            if level is not None: req['pressure_level']=[f'{level:g}']
            tasks.append({'dataset':'reanalysis-era5-'+('pressure-levels' if level is not None else 'single-levels'),'request':req})
        elif source=='carra2':
            names=[v.carra]
            if key.startswith('wind'): names += ['wind_direction' if key=='wind' else '10m_wind_direction']
            req=dict(base,product_type='analysis',variable=names,area=domain,data_format='netcdf',level_type='pressure_levels' if level is not None else 'single_levels')
            if level is not None: req['level_location']=[f'{level:g}']
            tasks.append({'dataset':'reanalysis-pan-carra','request':req})
        else:
            names=[v.merra]
            if key=='wind': names+=['V']
            if key=='wind10': names+=['V10M']
            tasks.append({'dataset':'M2I3NPASM' if level is not None else 'M2I1NXASM','version':'5.12.4','variables':names,'area':domain})
    plan = {'version':VERSION,'source':source,'variable':key,'time':t.isoformat().replace('+00:00','Z'),
            'level':level,'area':area,'requests':tasks,'time_kind':'instantaneous_analysis',
            'resolution_km':SOURCES[source]['resolution_km']}
    plan['key']=hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:24]
    return plan
