"""Готовые цветовые GeoTIFF/COG: просмотр без восстановления физических каналов."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import ColorInterp, Resampling
from rasterio.vrt import WarpedVRT

from .download import atomic_json, digest
from .geo import grid, lonlat_grid, solar_elevation
from .model import UTC, iso, parse_time
from .network import Cancelled

VERSION = 'archive-rgb-1'
NOTE = ('Готовая цветовая композиция поставщика. Цвета сохранены, каналы прибора '
        'из RGB не восстанавливаются. Температура, фаза облаков и условия '
        'обледенения по этому файлу не рассчитываются.')


def color_layout(ds):
    """Используем ColorInterp/палитру, а не принимаем три произвольных канала за RGB."""
    if ds.driver != 'GTiff' or not ds.crs or abs(ds.transform.determinant) == 0:
        raise ValueError('Для карты нужен GeoTIFF с системой координат и геопривязкой.')
    colors = list(ds.colorinterp)
    if all(c in colors for c in (ColorInterp.red, ColorInterp.green, ColorInterp.blue)):
        bands = [colors.index(c)+1 for c in (ColorInterp.red, ColorInterp.green, ColorInterp.blue)]
        mode = 'rgb'
    elif ds.count == 1 and colors[0] == ColorInterp.palette:
        ds.colormap(1)  # Ошибка отсутствующей палитры не должна маскироваться.
        bands, mode = [1], 'palette'
    else:
        raise ValueError('В GeoTIFF не объявлены цветовые полосы RGB или палитра. Нужны поканальные данные либо готовая RGB-композиция.')
    if any(ds.dtypes[b-1] not in ('uint8', 'uint16') for b in bands):
        raise ValueError('Готовые цвета поддерживаются в uint8/uint16; автоматическая растяжка физических величин не выполняется.')
    alpha = colors.index(ColorInterp.alpha)+1 if ColorInterp.alpha in colors else None
    if alpha and ds.dtypes[alpha-1] not in ('uint8', 'uint16'):
        raise ValueError('Неподдерживаемый тип альфа-канала.')
    return dict(mode=mode, bands=bands, alpha=alpha)


def check_tiff(path):
    # Проверка выполняется до GDAL: VRT может содержать внешние ссылки.
    with Path(path).open('rb') as stream:
        if stream.read(4) not in (b'II*\x00', b'MM\x00*', b'II+\x00', b'MM\x00+'):
            raise ValueError('Файл не является TIFF. XML/VRT под видом снимка не открывается.')


def inspect_composite(path, asset=None):
    """Метаданные срока берутся из каталога, TIFF-тегов или явно определённого имени."""
    path = Path(path).resolve()
    if path.suffix.lower() not in ('.tif', '.tiff'):
        return None
    check_tiff(path)
    asset = asset or {}
    with rasterio.open(path) as ds:
        tags = ds.tags()
        platform = asset.get('platform') or tags.get('platform', '')
        stamp, assumed = parse_time(asset.get('time') or tags.get('time', ''))
        match = re.fullmatch(r'A([12])_(\d{14})(?:[_\.].*)?\.tiff?', path.name, re.I)
        if match:
            if not platform:
                platform = 'ARCM'+match[1]
            if not stamp:
                stamp = iso(dt.datetime.strptime(match[2], '%Y%m%d%H%M%S').replace(tzinfo=UTC))
                assumed = True
        if platform not in ('ARCM1', 'ARCM2') or not stamp:
            return None
        layout = color_layout(ds)
        identity = asset.get('id') or hashlib.sha256(str(path).encode()).hexdigest()[:32]
        return dict(id=identity, asset_id=asset.get('id'), path=str(path), filename=path.name,
                    platform=platform, time=stamp, time_assumed=asset.get('time_assumed', assumed),
                    level=asset.get('level') or tags.get('level') or 'RGB',
                    crs=ds.crs.to_string(), size=path.stat().st_size, shape=[ds.height, ds.width],
                    dtype=ds.dtypes[layout['bands'][0]-1], **layout)


def register_composite(app, path, asset=None):
    entry = inspect_composite(path, asset)
    if entry is None:
        return None
    stamp = dt.datetime.fromisoformat(entry['time'].replace('Z', '+00:00'))
    identity = entry['platform']+'_'+stamp.strftime('%Y%m%d%H%M%S')
    with app.store.lock, app.store.conn:
        old = app.store.conn.execute('SELECT data FROM scenes WHERE id=?', (identity,)).fetchone()
        scene = json.loads(old[0]) if old else dict(id=identity, platform=entry['platform'],
                    time=entry['time'], time_assumed=entry['time_assumed'], channels={})
        # Повторный импорт той же копии не создаёт второй пункт списка.
        composites = scene.setdefault('composites', {})
        for key, previous in list(composites.items()):
            if key != entry['id'] and previous['path'] == entry['path']:
                del composites[key]
        if composites.get(entry['id']) == entry:
            return identity
        composites[entry['id']] = entry
        app.store.conn.execute('INSERT OR REPLACE INTO scenes VALUES(?,?,?)',
                              (identity, scene['time'], json.dumps(scene, ensure_ascii=False)))
        app.store.revision += 1
    return identity


def _byte(values, dtype):
    if dtype == 'uint16':
        return np.rint(values.astype('float32')/257).clip(0, 255).astype('uint8')
    return values.astype('uint8')


def read_composite(path, g):
    """WarpedVRT ограничивает чтение целевой сеткой; исходник целиком в RAM не загружается."""
    check_tiff(path)
    with rasterio.open(path) as ds:
        layout = color_layout(ds)
        table = ds.colormap(1) if layout['mode'] == 'palette' else None
        with WarpedVRT(ds, crs=g['crs'], transform=g['transform'], width=g['width'],
                       height=g['height'], resampling=Resampling.nearest,
                       add_alpha=layout['alpha'] is None, warp_mem_limit=128) as vrt:
            bands = vrt.read(layout['bands'])
            mask = vrt.dataset_mask()
            if table is not None:
                lut = np.zeros((65536, 4), dtype='uint8')
                for key, value in table.items():
                    lut[key] = value
                rgba = lut[bands[0]]
            else:
                rgb = np.stack([_byte(bands[i], ds.dtypes[b-1]) for i,b in enumerate(layout['bands'])], axis=-1)
                alpha = _byte(vrt.read(layout['alpha']), ds.dtypes[layout['alpha']-1]) if layout['alpha'] else mask
                rgba = np.dstack([rgb, alpha])
            rgba[:, :, 3] = np.minimum(rgba[:, :, 3], mask)
            rgba[rgba[:, :, 3] == 0, :3] = 0
    return rgba


def build_composite(scene, root, request, cancel=None, progress=lambda message: None):
    entries = scene.get('composites', {})
    selected = request.get('composite')
    if selected:
        source = entries.get(selected)
    else:
        source = next(iter(sorted(entries.values(), key=lambda e: (e['crs'] != 'EPSG:4326', e['size'], e['id']))), None)
    if not source or not Path(source['path']).is_file():
        raise ValueError('Готовая RGB-композиция не найдена на диске. Скачайте файл или заново откройте папку.')
    request = dict(request, product='archive_rgb', composite=source['id'])
    g = grid(request.get('preset', 'arctic'), request.get('width', 1000))
    path = Path(source['path']); before = path.stat()
    sha = digest(path, cancel)
    inputs = [dict(composite=source['id'], filename=path.name, sha256=sha, size=before.st_size,
                   source_crs=source['crs'])]
    identity = hashlib.sha256(json.dumps(dict(inputs=inputs, scene=scene['id'], time=scene['time'],
                              request=request, version=VERSION), sort_keys=True).encode()).hexdigest()[:24]
    final = Path(root)/identity
    if (final/'product.json').exists():
        return json.loads((final/'product.json').read_text(encoding='utf-8'))
    if cancel and cancel.is_set():
        raise Cancelled()
    progress('Нанесение готовой RGB-композиции: '+source['crs'])
    rgba = read_composite(path, g)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Исходный RGB-файл изменился во время чтения. Повторите построение.')
    valid = rgba[:, :, 3] > 0
    if not valid.any():
        raise ValueError('В выбранной области нет данных этой RGB-композиции. Измените область карты.')
    target = Path(root)/(identity+'.building')
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    try:
        profile = dict(driver='GTiff', width=g['width'], height=g['height'], crs=g['crs'],
                       transform=g['transform'], compress='deflate', tiled=True, blockxsize=256, blockysize=256)
        tags = dict(product='archive_rgb', scene=scene['id'], time=scene['time'], display_only='true',
                    source_sha256=sha, algorithm=VERSION)
        with rasterio.open(target/'display.tif', 'w', count=4, dtype='uint8', **profile) as ds:
            ds.write(rgba.transpose(2,0,1));ds.colorinterp=(ColorInterp.red,ColorInterp.green,ColorInterp.blue,ColorInterp.alpha)
            ds.update_tags(**tags)
        with rasterio.open(target/'quality.tif', 'w', count=1, dtype='uint8', **profile) as ds:
            ds.write((~valid).astype('uint8'), 1);ds.update_tags(**tags)
        Image.fromarray(rgba).save(target/'map.png')
        lon,lat = lonlat_grid(g);sun = solar_elevation(lon,lat,scene['time'])
        night = np.zeros_like(rgba);night[:,:,:3]=[13,26,50];night[:,:,3]=np.where(sun<0,90,0)
        Image.fromarray(night).save(target/'night.png')
        legend = dict(product='archive_rgb', title='Готовая RGB-композиция', units='RGB', status='not_applicable',
                      interpretation=dict(title='Цвета поставщика', purpose=NOTE, swatches=[]), meaning=NOTE,
                      source_filename=path.name, source_crs=source['crs'], source_level=source['level'],
                      nodata='Прозрачность — нет данных, не ясное небо.', quality_flags={'1':'нет данных'},
                      scene_time=scene['time'], version=VERSION, valid_pixels=int(valid.sum()), total_pixels=int(valid.size),
                      color_conversion='uint16 / 257 → uint8' if source['dtype']=='uint16' else 'Исходные 8-битные цвета; без растяжки')
        meta = dict(id=identity, scene_id=scene['id'], platform=scene['platform'], time=scene['time'],
                    product='archive_rgb', title='Архивный цветной снимок', display_only=True,
                    calibration_status='not_applicable', calibration_config={'mode':'unknown'},
                    time_assumed=scene.get('time_assumed', True), grid={k:v for k,v in g.items() if k!='transform'},
                    transform=list(g['transform'])[:6], request=request, inputs=inputs, legend=legend,
                    files=['map.png','night.png','display.tif','quality.tif','legend.json','product.json','report.html'])
        from .processing import write_report
        atomic_json(target/'legend.json', legend);write_report(target,meta,g)
        if cancel and cancel.is_set():
            raise Cancelled()
        atomic_json(target/'product.json',meta)
        os.replace(target,final)
        return meta
    finally:
        if target.exists():
            shutil.rmtree(target)
