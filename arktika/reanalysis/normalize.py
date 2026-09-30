"""Выбор точного времени/уровня и нормализация единиц, без догадок по величинам."""
from __future__ import annotations
from pathlib import Path
import datetime as dt
import json
import re
import numpy as np
from .catalog import VARIABLES, VERSION

MAX_POINTS = 9_000_000
CF_UNITS={'t':'degC','t2m':'degC','sst':'degC','q':'g kg-1','rh':'%','ice':'%','z':'m','wind':'m s-1','wind10':'m s-1','omega':'Pa s-1','mslp':'hPa'}
CF_NAMES={'t':'air_temperature','t2m':'air_temperature','sst':'sea_surface_temperature','q':'specific_humidity','rh':'relative_humidity','ice':'sea_ice_area_fraction','z':'geopotential_height','wind':'wind_speed','wind10':'wind_speed','omega':'lagrangian_tendency_of_air_pressure','mslp':'air_pressure_at_mean_sea_level'}


def _coord(ds, names, standard=None):
    for name in names:
        if name in ds: return ds[name]
    for name, v in ds.variables.items():
        if standard and v.attrs.get('standard_name')==standard: return ds[name]
    raise ValueError('В файле отсутствует координата '+names[0]+'.')


def _variable(ds, names):
    lower={str(k).lower():k for k in ds.data_vars}
    for name in names:
        if str(name).lower() in lower: return ds[lower[str(name).lower()]]
    for k in ds.data_vars:
        if ds[k].attrs.get('standard_name') in names: return ds[k]
    raise ValueError('В файле отсутствует переменная: '+', '.join(str(x) for x in names)+'.')


def unit_text(v):
    return str(v.attrs.get('units','')).lower().strip().replace('**','').replace('^','').replace(' ','').replace('−','-')


def convert(v, kind):
    a = np.asarray(v.values,dtype=np.float64)
    u = unit_text(v)
    # CF decoding must have been applied by xarray. No assumptions from DN ranges.
    if kind in ('t','t2m','sst'):
        if u in ('k','kelvin','kelvins'): a-=273.15
        elif u not in ('c','degc','celsius','degree_celsius','degrees_celsius','°c'): raise ValueError('Неизвестные единицы температуры: '+u)
    elif kind=='mslp':
        if u in ('pa','pascal','pascals'): a/=100.
        elif u not in ('hpa','mbar','millibar','millibars'): raise ValueError('Неизвестные единицы давления: '+u)
    elif kind=='z':
        if u in ('m2s-2','m2/s2','m2s**-2'): a/=9.80665
        elif u not in ('m','gpm','meter','metre','meters','metres'): raise ValueError('Неизвестные единицы геопотенциала/высоты: '+u)
    elif kind=='q':
        if u in ('kgkg-1','kg/kg','1'): a*=1000.
        elif u not in ('gkg-1','g/kg'): raise ValueError('Неизвестные единицы удельной влажности: '+u)
    elif kind in ('rh','ice'):
        if u in ('1','fraction','(0-1)'): a*=100.
        elif u not in ('%','percent','percentage'): raise ValueError('Неизвестные единицы доли/влажности: '+u)
    elif kind in ('wind','wind10','u','v'):
        if u not in ('ms-1','m/s','metersecond-1','metrespersecond'): raise ValueError('Ветер должен иметь единицы м/с, не узлы и не градусы.')
    elif kind=='omega':
        if u not in ('pas-1','pa/s'): raise ValueError('Для ω нужны Па/с; геометрическая скорость м/с не эквивалентна ω.')
    elif kind=='direction':
        if u not in ('degree','degrees','deg','degrees_true','degree_true','°'): raise ValueError('Неизвестные единицы направления ветра.')
    a[~np.isfinite(a)]=np.nan
    return a.astype('float32')


def exact_slice(ds, plan):
    """CF time is mandatory; equal nearest-hour substitutes are never accepted."""
    time = _coord(ds,('valid_time','time'),'time')
    times = np.asarray(time.values).reshape(-1)
    if not np.issubdtype(times.dtype,np.datetime64):
        raise ValueError('Время не декодировано как CF datetime64; проверьте календарь и units.')
    wanted=np.datetime64(plan['time'].replace('Z',''),'ns')
    hits=np.flatnonzero(times.astype('datetime64[ns]')==wanted)
    if hits.size!=1: raise ValueError('В файле нет единственного точного срока '+plan['time']+'.')
    if time.ndim>1: raise ValueError('Многомерное прогнозное время не поддерживается; нужны сроки анализа.')
    if time.dims: ds=ds.isel({time.dims[0]:int(hits[0])})
    if 'step' in ds and np.any(np.asarray(ds['step'].values)!=np.timedelta64(0,'ns')):
        raise ValueError('Ненулевая заблаговременность: прогноз нельзя назвать анализом.')
    if plan['level'] is not None:
        lev=_coord(ds,('pressure_level','isobaricInhPa','plev','lev','level_location','level'),'air_pressure')
        values=np.asarray(lev.values,dtype=float).reshape(-1)
        units=unit_text(lev)
        if units in ('pa','pascal'): values/=100.
        elif units not in ('hpa','mbar','millibar') and lev.name not in ('pressure_level','isobaricInhPa'):
            raise ValueError('У координаты давления нет подтверждённых единиц Па/гПа.')
        hits=np.flatnonzero(np.isclose(values,plan['level'],rtol=0,atol=1e-5))
        if hits.size!=1: raise ValueError('Запрошенный уровень отсутствует или неоднозначен.')
        if lev.dims: ds=ds.isel({lev.dims[0]:int(hits[0])})
    elif any(name in ds.dims and ds.sizes[name]>1 for name in ('pressure_level','isobaricInhPa','plev','lev')):
        raise ValueError('Поверхностное поле неожиданно содержит несколько уровней давления.')
    for v in ds.data_vars.values():
        methods=str(v.attrs.get('cell_methods','')).lower()
        if re.search(r'time\s*:\s*(mean|sum|minimum|maximum|variance|standard_deviation)',methods) or str(v.attrs.get('GRIB_stepType','instant')).lower() in ('avg','accum','max','min'):
            raise ValueError('Поле усреднено или накоплено по времени; нужен мгновенный анализ.')
    return ds


def spatial_slice(ds, area):
    """Subset remote rectangular arrays before .values loads their data."""
    lat=_coord(ds,('latitude','lat'),'latitude');lon=_coord(ds,('longitude','lon'),'longitude')
    if lat.size>MAX_POINTS or lon.size>MAX_POINTS: raise ValueError('Слишком большая сетка; запросите меньшую область.')
    n,w,s,e=area
    if lat.ndim==lon.ndim==1 and lat.dims!=lon.dims:
        la=np.asarray(lat.values);lo=(np.asarray(lon.values)+180)%360-180
        yi=np.flatnonzero((la>=s)&(la<=n));xi=np.flatnonzero(((lo>=w)&(lo<=e)) if w<e else ((lo>=w)|(lo<=e)))
        if not yi.size or not xi.size: raise ValueError('Область не пересекает данные.')
        # Broad contiguous slice for DAP; longitude mask is applied after download.
        ds=ds.isel({lat.dims[0]:slice(int(yi.min()),int(yi.max())+1),lon.dims[0]:slice(int(xi.min()),int(xi.max())+1)})
    return ds


def normalize(ds, plan, area=None):
    import xarray as xr
    ds=spatial_slice(exact_slice(ds,plan),area or plan['area'])
    key=plan['variable'];spec=VARIABLES[key];source=plan['source']
    native_export=ds.attrs.get('version')==VERSION and ds.attrs.get('source')==source and ds.attrs.get('variable')==key and not ds.attrs.get('difference_of')
    lat=_coord(ds,('latitude','lat'),'latitude');lon=_coord(ds,('longitude','lon'),'longitude')
    if lat.ndim==lon.ndim==1 and lat.dims!=lon.dims:
        lon2,lat2=np.meshgrid(lon.values,lat.values);dims=(lat.dims[0],lon.dims[0])
    elif lat.ndim==lon.ndim==2 and lat.dims==lon.dims:
        lat2=lat.values;lon2=lon.values;dims=lat.dims
    else: raise ValueError('Поддерживаются регулярная lat/lon и двумерная курвилинейная сетки.')
    if lat2.size>MAX_POINTS: raise ValueError('Поле превышает лимит узлов.')
    lon2=(np.asarray(lon2,dtype=float)+180)%360-180;lat2=np.asarray(lat2,dtype=float)
    def take(names):
        v=_variable(ds,[x for x in names if x])
        for dim in list(v.dims):
            if dim not in dims:
                if v.sizes[dim]!=1: raise ValueError('Не выбран размер '+dim+'; ансамбли и многослойные файлы не смешиваются.')
                v=v.isel({dim:0})
        if set(v.dims)!=set(dims): raise ValueError('Переменная не расположена на двумерной координатной сетке.')
        return v.transpose(*dims)
    attrs={'source':source,'variable':key,'time':plan['time'],'level':json.dumps(plan['level']),
           'unit':spec.unit,'Conventions':'CF-1.8','geolocation_crs':'EPSG:4326','version':VERSION,'time_kind':'instantaneous_analysis',
           'resolution_km':plan['resolution_km'],'sampling':'nearest_native_cell','area':json.dumps(plan['area'])}
    vectors={}
    if key.startswith('wind') and source=='carra2' and not native_export:
        speed=convert(take([spec.carra,*spec.aliases]),key)
        direction_var=take(['wind_direction' if key=='wind' else '10m_wind_direction','wdir','dd','wdir10','10wdir','wind_from_direction'])
        if int(direction_var.attrs.get('GRIB_uvRelativeToGrid',0))!=0 or 'grid' in str(direction_var.attrs.get('standard_name','')).lower():
            raise ValueError('Направление CARRA2 объявлено относительно сетки, а не географического севера.')
        direction=convert(direction_var,'direction')
        if np.any(np.isfinite(speed)&(speed<0)): raise ValueError('Скорость ветра не может быть отрицательной.')
        if np.any(np.isfinite(direction)&((direction<0)|(direction>360))): raise ValueError('Направление ветра вне 0…360°.')
        u=-speed*np.sin(np.deg2rad(direction));v=-speed*np.cos(np.deg2rad(direction))
        values=speed;vectors={'u':u,'v':v};attrs['wind_reference']='geographic_from_direction'
    elif key.startswith('wind'):
        uvar=take([spec.merra if source=='merra2' else spec.cds,*spec.aliases])
        vnames=['V','v','northward_wind','v_component_of_wind'] if key=='wind' else ['V10M','v10','10v','10m_v_component_of_wind']
        vvar=take(vnames)
        for component in (uvar,vvar):
            if int(component.attrs.get('GRIB_uvRelativeToGrid',0))!=0:
                raise ValueError('Ветер задан относительно сетки; требуется подтверждённый поворот в географическую систему.')
        u=convert(uvar,'u');v=convert(vvar,'v');values=np.hypot(u,v);vectors={'u':u,'v':v};attrs['wind_reference']='east_north'
    else:
        values=convert(take(['value'] if native_export else [spec.merra if source=='merra2' else spec.carra if source=='carra2' else spec.cds,*spec.aliases]),key)
    n,w,s,e=area or plan['area'];mask=np.isfinite(lat2)&np.isfinite(lon2)&(lat2>=s)&(lat2<=n)
    mask &= ((lon2>=w)&(lon2<=e)) if w<e else ((lon2>=w)|(lon2<=e))
    if not mask.any(): raise ValueError('После пространственного отбора нет данных.')
    ys,xs=np.where(mask);sl=(slice(ys.min(),ys.max()+1),slice(xs.min(),xs.max()+1))
    values=np.where(mask,values,np.nan)[sl]
    if not np.isfinite(values).any(): raise ValueError('Выбранное поле полностью отсутствует в области.')
    fields={'value':(('y','x'),values),'latitude':(('y','x'),lat2[sl]),'longitude':(('y','x'),lon2[sl])}
    for name,array in vectors.items(): fields[name]=(('y','x'),np.where(mask,array,np.nan)[sl].astype('float32'))
    result=xr.Dataset(fields,attrs=attrs)
    result['value'].attrs.update(units=CF_UNITS[key],long_name=spec.title,standard_name=CF_NAMES[key])
    for component,name in [('u','eastward_wind'),('v','northward_wind')]:
        if component in result: result[component].attrs.update(units='m s-1',standard_name=name)
    result.latitude.attrs.update(units='degrees_north',standard_name='latitude')
    result.longitude.attrs.update(units='degrees_east',standard_name='longitude')
    result=result.set_coords(['latitude','longitude']).assign_coords(time=np.datetime64(plan['time'].replace('Z',''),'ns'))
    result.time.attrs.update(standard_name='time')
    if plan['level'] is not None:
        result=result.assign_coords(pressure_level=plan['level']);result.pressure_level.attrs.update(units='hPa',standard_name='air_pressure',positive='down')
    elif key in ('t2m','wind10'):
        result=result.assign_coords(height=2. if key=='t2m' else 10.);result.height.attrs.update(units='m',standard_name='height',positive='up')
    return result


def save_native(ds, target):
    """NetCDF4 compressed in deployment; NetCDF3 fallback in minimal test environments."""
    try: import netCDF4
    except ImportError: ds.to_netcdf(target,engine='scipy')
    else: ds.to_netcdf(target,engine='netcdf4',encoding={k:{'zlib':True,'complevel':3} for k in ds.variables if ds[k].ndim>0})
