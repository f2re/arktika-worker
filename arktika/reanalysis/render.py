"""Общая геометрия слоёв: ближайший исходный узел; без заполнения пропусков."""
from __future__ import annotations
import math
from pathlib import Path
import numpy as np
from pyproj import Geod, Transformer
from ..geo import grid, lonlat_grid
from ..download import atomic_json
from .catalog import FIELDS, identity, number

EARTH_KM = 6371.0088


def xyz(lon, lat):
    lo, la = np.deg2rad(lon), np.deg2rad(lat)
    c = np.cos(la)
    return np.column_stack((c.ravel() * np.cos(lo).ravel(), c.ravel() * np.sin(lo).ravel(), np.sin(la).ravel()))


def support(arrays, meta, lon, lat, distance):
    # Threshold is below one full grid diagonal: a remote land/sea value cannot fill a missing cell.
    valid = np.isfinite(lon + lat) & (distance <= meta['resolution_km'] * .82)
    n, w, s, e = meta['request']['area']
    valid &= (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
    if meta.get('regular'):
        valid &= (lat >= np.nanmin(arrays['lat'])) & (lat <= np.nanmax(arrays['lat']))
        # Native cropped geographic grid, including the two sides of a dateline.
        span = np.nanmax(arrays['lon']) - np.nanmin(arrays['lon'])
        if span < 359:
            valid &= (lon >= np.nanmin(arrays['lon'])) & (lon <= np.nanmax(arrays['lon']))
    return valid


def sample(arrays, meta, lon, lat, tree=True):
    lon, lat = np.broadcast_arrays(np.asarray(lon, float), np.asarray(lat, float))
    shape = lon.shape
    lo, la = lon.ravel(), lat.ravel()
    valid_geo = np.isfinite(arrays['lat'] + arrays['lon']).ravel()
    ids = np.flatnonzero(valid_geo)
    if not len(ids):
        raise ValueError('Нет корректной геолокации поля.')
    source_xyz = xyz(arrays['lon'], arrays['lat'])[valid_geo]
    targets = xyz(np.where(np.isfinite(lo), lo, 0), np.where(np.isfinite(la), la, 0))
    if tree:
        from scipy.spatial import cKDTree
        chord, local = cKDTree(source_xyz).query(targets, workers=1)
    else:
        # Point inspection does not require the optional worker runtime or scipy in the server.
        if len(targets) != 1:
            raise ValueError('Инспектор принимает одну точку.')
        ds = np.sum((source_xyz - targets[0]) ** 2, axis=1)
        local = np.array([int(np.argmin(ds))]); chord = np.sqrt(ds[local])
    distance = 2 * EARTH_KM * np.arcsin(np.clip(chord / 2, 0, 1))
    good = support(arrays, meta, lo, la, distance)
    indices = ids[local]
    result = {k: np.where(good, arrays[k].ravel()[indices], np.nan).reshape(shape) for k in arrays}
    result['distance_km'] = np.where(good, distance, np.nan).reshape(shape)
    return result


def difference(left, lm, right, rm):
    if lm.get('difference') or rm.get('difference'):
        raise ValueError('Разности строятся между двумя исходными полями.')
    for key in ('field', 'level', 'time', 'units', 'temporal'):
        if lm[key] != rm[key]:
            raise ValueError('Для разности должны совпадать поле, уровень, точный срок, единицы и временное осреднение.')
    if lm['vector'] or rm['vector']:
        raise ValueError('Выберите поле «Скорость ветра» для скалярной разности; компоненты не усредняются.')
    base, bm = (left, lm) if lm['resolution_km'] >= rm['resolution_km'] else (right, rm)
    a = sample(left, lm, base['lon'], base['lat'])['value']
    b = sample(right, rm, base['lon'], base['lat'])['value']
    delta = np.where(np.isfinite(a) & np.isfinite(b), a - b, np.nan).astype('float32')
    if not np.isfinite(delta).any():
        raise ValueError('Нет общей области с данными; пустая разность не создана.')
    meta = dict(bm, source='difference', name=lm['name'] + ' − ' + rm['name'],
                title=lm['title'],
                difference=True, vector=False, valid_cells=int(np.isfinite(delta).sum()),
                units='K' if FIELDS[lm['field']]['kind'] == 'temperature' else lm['units'],
                provenance={'operation': 'A-B', 'inputs': [{'id': m['id'], 'sha256': m['sha256']} for m in (lm, rm)],
                            'method': 'nearest_on_coarser_native_grid', 'target_source': bm['source'],
                            'note': 'Разность не является погрешностью: ни один реанализ не принят за истину.'})
    meta.pop('sha256', None); meta.pop('netcdf_sha256', None)
    return {'lat': base['lat'], 'lon': base['lon'], 'value': delta}, meta


def palette(values, lo, hi, diverging=False):
    # UI palette, not a change to numerical values. Missing cells are transparent.
    from PIL import Image
    colors = np.array([[40, 77, 150], [117, 178, 208], [247, 247, 240], [233, 169, 108], [162, 45, 49]], float) if diverging else np.array([[25, 56, 109], [36, 127, 155], [83, 178, 152], [224, 215, 121], [198, 80, 49]], float)
    z = np.clip((np.nan_to_num(values, nan=lo) - lo) / (hi - lo), 0, 1)
    rgba = np.empty((*z.shape, 4), dtype='uint8')
    for k in range(3): rgba[..., k] = np.interp(z, np.linspace(0, 1, len(colors)), colors[:, k]).astype('uint8')
    rgba[..., 3] = np.where(np.isfinite(values), 255, 0)
    return Image.fromarray(rgba, 'RGBA'), colors.astype(int).tolist()


def style(data, meta):
    spec = FIELDS[meta['field']]
    result = {'mode': data.get('mode', 'raster'), 'opacity': number(data.get('opacity', .55), 'Непрозрачность')}
    if result['mode'] not in ('raster', 'contours', 'wind') or result['mode'] == 'wind' and not meta['vector']:
        raise ValueError('Недопустимый способ отображения этого поля.')
    if not math.isfinite(result['opacity']) or not 0 <= result['opacity'] <= 1:
        raise ValueError('Прозрачность: 0–1.')
    default = [-10, 10] if meta.get('difference') else spec['range']
    result['min'] = number(data.get('min', default[0]), 'Нижняя граница'); result['max'] = number(data.get('max', default[1]), 'Верхняя граница')
    result['step'] = number(data.get('step', 2 if meta.get('difference') else spec['step']), 'Шаг изолиний')
    if not all(math.isfinite(result[k]) for k in ('min', 'max', 'step')) or not result['min'] < result['max'] or result['step'] <= 0:
        raise ValueError('Нужны конечные границы шкалы (от < до) и положительный шаг изолиний.')
    if (result['max'] - result['min']) / result['step'] > 100:
        raise ValueError('Не более 100 изолиний; увеличьте шаг.')
    return result


def render(arrays, meta, preset, width, options, root):
    import contourpy
    import rasterio
    g = grid(preset, width)
    opts = style(options, meta)
    key = identity({'field': meta['sha256'], 'id': meta['id'], 'preset': preset, 'width': width,
                    'scale': [opts[k] for k in ('min', 'max', 'step')], 'version': 'overlay-1'})
    folder = Path(root) / key
    existing = folder / 'render.json'
    if existing.is_file() and (folder/'map.png').is_file() and (folder/'values.tif').is_file():
        import json
        return json.loads(existing.read_text(encoding='utf-8'))
    lon, lat = lonlat_grid(g)
    picked = sample(arrays, meta, lon, lat)
    values = picked['value']
    image, colors = palette(values, opts['min'], opts['max'], meta.get('difference', False) or FIELDS[meta['field']]['kind'] == 'temperature')
    folder.mkdir(parents=True, exist_ok=True)
    image.save(folder / 'map.png')
    with rasterio.open(folder / 'values.tif', 'w', driver='GTiff', height=g['height'], width=g['width'], count=1,
                       dtype='float32', crs=g['crs'], transform=g['transform'], nodata=float('nan'), compress='deflate') as ds:
        ds.write(values.astype('float32'), 1)
        ds.update_tags(units=meta['units'], valid_time=meta['time'], field_id=meta['id'], method='nearest_native_node')
    contours = []
    if np.isfinite(values).sum() > 3:
        contour = contourpy.contour_generator(x=np.arange(g['width']) + .5, y=np.arange(g['height']) + .5,
                                               z=np.ma.masked_invalid(values), corner_mask=False)
        for level in np.arange(opts['min'], opts['max'] + opts['step'] * .01, opts['step']):
            for line in contour.lines(float(level)):
                if len(line) > 1:
                    contours.append({'value': round(float(level), 5), 'points': np.round(line, 2).tolist()})
    vectors = []
    if meta['vector']:
        geod = Geod(ellps='WGS84'); tr = Transformer.from_crs(4326, g['crs'], always_xy=True)
        for y in range(16, g['height'], 36):
            for x in range(16, g['width'], 36):
                u, v = picked['u'][y, x], picked['v'][y, x]
                if not np.isfinite(u + v): continue
                speed = float(np.hypot(u, v))
                if speed < .2: continue
                lon2, lat2, _ = geod.fwd(lon[y, x], lat[y, x], math.degrees(math.atan2(u, v)), 1000)
                xx, yy = tr.transform(lon2, lat2)
                px, py = (~g['transform']) * (xx, yy)
                dx, dy = px - x - .5, py - y - .5
                norm = math.hypot(dx, dy)
                if not math.isfinite(norm) or norm < 1e-8: continue
                vectors.append(dict(x=x + .5, y=y + .5, dx=round(dx / norm * 23, 2), dy=round(dy / norm * 23, 2), speed=round(speed, 2)))
    result = dict(id=key, field_id=meta['id'], preset=preset, width=g['width'], height=g['height'], crs=g['crs'],
                  bounds=g['bounds'], contours=contours, vectors=vectors, image='/reanalysis/render/' + key + '/map.png',
                  legend={'title': meta['title'], 'units': meta['units'], 'min': opts['min'], 'max': opts['max'], 'colors': colors,
                          'method': 'Ближайший исходный узел. Увеличение карты не повышает разрешение реанализа.'},
                  valid_pixels=int(np.isfinite(values).sum()))
    atomic_json(existing, result)
    return result
