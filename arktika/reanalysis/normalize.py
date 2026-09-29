"""Проверка NetCDF/CF: точный срок, уровень, единицы, геолокация и маски."""
from __future__ import annotations
import numpy as np
from .catalog import ALIASES, FIELDS, SOURCES, stamp

MAX_CELLS = 10_000_000


def coordinate(ds, names, standards=()):
    for name in names:
        if name in ds.variables:
            return name
    for name in ds.variables:
        if ds[name].attrs.get('standard_name') in standards:
            return name
    raise ValueError('В файле отсутствует координата ' + names[0] + '.')


def select(ds, r):
    """Do selection before loading data; works with a lazy OPeNDAP dataset."""
    import xarray as xr
    for key in ('step','forecast_period'):
        if key in ds.variables:
            value=np.asarray(ds[key].values)
            if np.issubdtype(value.dtype,np.timedelta64):
                nonzero=np.any(value!=np.timedelta64(0,'s'))
            else:
                nonzero=np.any(value!=0)
            if nonzero:
                raise ValueError('Файл содержит прогноз, а выбран анализ реанализа; заблаговременность не подменяется.')
    time_name = coordinate(ds, ['valid_time', 'time'], ['time'])
    times = np.asarray(ds[time_name].values).reshape(-1)
    if not np.issubdtype(times.dtype, np.datetime64):
        raise ValueError('Время NetCDF не декодируется как календарь UTC.')
    expected = np.datetime64(stamp(r['time']).replace(tzinfo=None), 'ns')
    hits = np.flatnonzero(times.astype('datetime64[ns]') == expected)
    if len(hits) != 1:
        raise ValueError('Файл не содержит единственный точный запрошенный срок. Другой час не используется.')
    tc = ds[time_name]
    if tc.ndim == 1:
        ds = ds.isel({tc.dims[0]: int(hits[0])})
    elif tc.ndim > 1:
        raise ValueError('Неоднозначная сетка времени/заблаговременности. Выберите один срок анализа.')
    if r['level'] is not None:
        lev = coordinate(ds, ['pressure_level', 'isobaricInhPa', 'level', 'lev', 'plev'])
        vals = np.asarray(ds[lev].values).reshape(-1).astype(float)
        unit = str(ds[lev].attrs.get('units', '')).lower()
        if unit in ('pa', 'pascal', 'pascals'):
            vals /= 100
        elif unit not in ('hpa', 'millibar', 'millibars', 'mbar', 'mb') and lev != 'isobaricInhPa':
            raise ValueError('Единицы вертикальной координаты не заданы в Па/гПа.')
        ii = np.flatnonzero(np.isclose(vals, r['level'], atol=1e-5, rtol=0))
        if len(ii) != 1:
            raise ValueError('Запрошенного изобарического уровня нет в файле.')
        if ds[lev].ndim == 1:
            ds = ds.isel({ds[lev].dims[0]: int(ii[0])})
        elif ds[lev].ndim > 1:
            raise ValueError('Нужна изобарическая, а не гибридная вертикальная координата.')
    lat = coordinate(ds, ['latitude', 'lat'], ['latitude'])
    lon = coordinate(ds, ['longitude', 'lon'], ['longitude'])
    la, lo = ds[lat], ds[lon]
    for coord, allowed in ((la, ('degrees_north','degree_north','degrees_n','degree_n')), (lo, ('degrees_east','degree_east','degrees_e','degree_e'))):
        if str(coord.attrs.get('units', '')).lower() not in allowed:
            raise ValueError('Геолокация должна иметь явно заданные единицы градусов широты/долготы.')
    if la.ndim not in (1, 2) or lo.ndim != la.ndim:
        raise ValueError('Поддерживаются регулярная широта/долгота и двумерная геолокация CF.')
    if la.ndim == 1 and la.dims == lo.dims:
        raise ValueError('Список несвязанных точек вместо двумерной сетки не поддерживается.')
    n, w, s, e = r['area']
    if la.ndim == 1:
        latvals = np.asarray(la.values, float)
        lonvals = ((np.asarray(lo.values, float) + 180) % 360) - 180
        iy = np.flatnonzero((latvals >= s) & (latvals <= n))
        ix = np.flatnonzero((lonvals >= w) & (lonvals <= e))
        if not len(ix) or not len(iy):
            raise ValueError('В запрошенной области нет узлов сетки.')
        # Lat/lon slices are applied remotely by pydap; a wrapped longitude window may be split.
        ds = ds.isel({la.dims[0]: slice(int(iy.min()), int(iy.max()) + 1),
                      lo.dims[0]: slice(int(ix.min()), int(ix.max()) + 1)})
    else:
        if la.dims != lo.dims or la.shape != lo.shape:
            raise ValueError('Координаты широты и долготы имеют разные сетки.')
        if la.size > MAX_CELLS:
            raise ValueError('Слишком большая геолокационная сетка.')
        lav = np.asarray(la.values, float)
        lov = ((np.asarray(lo.values, float) + 180) % 360) - 180
        inside = np.isfinite(lav + lov) & (lav >= s) & (lav <= n) & (lov >= w) & (lov <= e)
        yy, xx = np.nonzero(inside)
        if not len(xx):
            raise ValueError('Область не пересекает фактическую сетку источника.')
        ds = ds.isel({la.dims[0]: slice(int(yy.min()), int(yy.max()) + 1),
                      la.dims[1]: slice(int(xx.min()), int(xx.max()) + 1)})
    la, lo = xr.broadcast(ds[lat], ds[lon])
    if la.size > MAX_CELLS:
        raise ValueError('Более 10 миллионов узлов: уменьшите область.')
    return ds, la, lo


def variable(ds, name):
    for key in [name] + ALIASES.get(name, []):
        if key in ds.data_vars:
            return ds[key]
    raise ValueError('Источник не вернул переменную ' + name + '.')


def convert(values, units, kind, name):
    """No value-range guessing: an unknown unit is an error."""
    u = str(units).lower().replace(' ', '').replace('**', '^').replace('−', '-').replace('°', 'deg')
    a = np.asarray(values, dtype=np.float64)
    if kind == 'temperature':
        if u in ('k', 'kelvin', 'kelvins'): return a - 273.15
        if u in ('degc', 'c', 'celsius', 'degree_celsius', 'degrees_celsius'): return a
    if kind == 'height':
        if u in ('m^2s^-2', 'm2s-2', 'm^2/s^2', 'm2/s2'): return a / 9.80665
        if u in ('m', 'gpm', 'meter', 'metres', 'meters'): return a
    if kind == 'pressure':
        if u in ('pa', 'pascal', 'pascals'): return a / 100
        if u in ('hpa', 'mb', 'mbar', 'millibars'): return a
    if kind in ('humidity', 'fraction'):
        if u in ('%', 'percent', 'percentage'): return a
        if u in ('1', 'dimensionless', 'fraction'): return a * 100
    if kind in ('wind', 'speed') and u in ('ms-1', 'ms^-1', 'm/s'): return a
    if kind == 'omega' and u in ('pas-1', 'pas^-1', 'pa/s'): return a
    raise ValueError(f'{name}: неподдерживаемые или отсутствующие единицы {units!r}.')


def normalize(ds, r):
    ds, la, lo = select(ds, r)
    spec = FIELDS[r['field']]
    arrays, original = [], []
    for name in spec[r['source']]:
        v = variable(ds, name)
        # Singleton auxiliary dimensions (expver etc.) are safe, but multiple versions are not silently merged.
        for dim in list(v.dims):
            if dim not in la.dims:
                if v.sizes[dim] != 1:
                    raise ValueError('В файле несколько реализаций/уровней: ' + dim + '.')
                v = v.isel({dim: 0})
        if set(v.dims) != set(la.dims):
            raise ValueError('Размерности поля не совпадают с геолокацией.')
        v = v.transpose(*la.dims)
        if 'time:' in str(v.attrs.get('cell_methods', '')):
            averaged = 'time: mean' in str(v.attrs['cell_methods'])
            if averaged != (r['temporal'] == 'mean_1h'):
                raise ValueError('Временное осреднение в файле не соответствует запросу.')
        if spec['kind'] == 'wind' and v.attrs.get('GRIB_uvRelativeToGrid', 0) not in (0, '0'):
            raise ValueError('Компоненты ветра относительно сетки требуют поворота; географические стрелки не выдумываются.')
        original.append(dict(name=v.name, units=str(v.attrs.get('units', ''))))
        arrays.append(convert(v.values, v.attrs.get('units', ''), spec['kind'], name))
    lat = np.asarray(la.values, dtype='float64')
    lon = (np.asarray(lo.values, dtype='float64') + 180) % 360 - 180
    n, w, s, e = r['area']
    mask = np.isfinite(lat + lon) & (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
    for a in arrays:
        mask &= np.isfinite(a)
    if not mask.any():
        raise ValueError('Запрошенное поле целиком отсутствует; пустой слой не добавлен.')
    arrays = [np.where(mask, a, np.nan).astype('float32') for a in arrays]
    scalar = np.hypot(*arrays) if len(arrays) == 2 else arrays[0]
    meta = dict(request=r, source=r['source'], name=SOURCES[r['source']]['name'], field=r['field'],
                title=spec['name'], units=spec['units'], level=r['level'], time=r['time'], temporal=r['temporal'],
                resolution_km=SOURCES[r['source']]['resolution_km'], original_variables=original,
                shape=list(scalar.shape), valid_cells=int(mask.sum()), vector=spec['kind'] == 'wind',
                regular=bool(np.allclose(lat, lat[:, :1], equal_nan=True) and np.allclose(lon, lon[:1, :], equal_nan=True)))
    return dict(lat=lat, lon=lon, value=scalar, **({'u': arrays[0], 'v': arrays[1]} if meta['vector'] else {})), meta
