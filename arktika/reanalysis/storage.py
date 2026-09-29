"""Атомарный кэш численных полей. Статус готовности публикуется последним."""
from __future__ import annotations
import json
import re
from pathlib import Path
import numpy as np
from ..download import atomic_json, digest
from .catalog import VERSION


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{24}', value):
        raise ValueError('Неверный идентификатор поля.')
    return value


def load(root, identity):
    folder = Path(root) / safe_id(identity)
    try:
        meta = json.loads((folder / 'field.json').read_text(encoding='utf-8'))
        if meta['id'] != identity or meta['version'] != VERSION:
            raise ValueError('Версия кэша изменилась; пересоздайте поле.')
        if digest(folder / 'field.npz') != meta['sha256']:
            raise ValueError('Контрольная сумма кэша не совпала; повторите загрузку.')
        with np.load(folder / 'field.npz', allow_pickle=False) as data:
            arrays = {key: data[key] for key in data.files}
        return arrays, meta
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        raise ValueError('Поле отсутствует или не завершено. Повторите загрузку.') from None


def save(folder, arrays, meta):
    import xarray as xr
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(folder / 'field.npz', **arrays)
    variables = {k: (('y', 'x'), a) for k, a in arrays.items() if k not in ('lat', 'lon')}
    ds = xr.Dataset(variables, coords={'lat': (('y', 'x'), arrays['lat']), 'lon': (('y', 'x'), arrays['lon'])})
    ds['lat'].attrs.update(standard_name='latitude', units='degrees_north')
    ds['lon'].attrs.update(standard_name='longitude', units='degrees_east')
    ds['value'].attrs.update(units={'°C':'degC','м':'m','гПа':'hPa','м/с':'m s-1','Па/с':'Pa s-1'}.get(meta['units'],meta['units']), long_name=meta['title'])
    for name in ('u', 'v'):
        if name in ds: ds[name].attrs['units'] = 'm s-1'
    ds=ds.assign_coords(time=np.datetime64(meta['time'].replace('Z',''), 'ns'))
    ds['time'].attrs['standard_name']='time'
    if meta.get('level') is not None:
        ds=ds.assign_coords(pressure_level=float(meta['level']))
        ds['pressure_level'].attrs.update(standard_name='air_pressure',units='hPa',positive='down')
    ds['value'].attrs['cell_methods']='time: mean' if meta['temporal']=='mean_1h' else 'time: point'
    ds.attrs.update(Conventions='CF-1.10', provenance=json.dumps(meta, ensure_ascii=False), valid_time=meta['time'])
    # The runtime has netCDF4; scipy permits offline contract tests without the optional runtime.
    try:
        import netCDF4  # noqa: F401
        engine = 'netcdf4'
    except ImportError:
        engine = 'scipy'
    encoding = {name: {'zlib': True, 'complevel': 4} for name in ds.data_vars} if engine == 'netcdf4' else {}
    ds.to_netcdf(folder / 'field.nc', engine=engine, encoding=encoding)
    meta = dict(meta, sha256=digest(folder / 'field.npz'), netcdf_sha256=digest(folder / 'field.nc'), version=VERSION)
    atomic_json(folder / 'field.json', meta)
    return meta


def listing(root):
    rows = []
    for p in Path(root).glob('*/field.json'):
        if not re.fullmatch('[0-9a-f]{24}', p.parent.name):
            continue
        try:
            obj = json.loads(p.read_text(encoding='utf-8'))
            if (p.parent / 'field.npz').is_file(): rows.append(obj)
        except (OSError, ValueError):
            continue
    return sorted(rows, key=lambda r: r.get('created_at', ''), reverse=True)
