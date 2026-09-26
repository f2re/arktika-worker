"""Явная шкала GeoTIFF/STAC и исследовательское сопоставление с опорами.

Ни экстремумы изображения, ни имя L2IR не определяют температуру. Линейная
модель здесь относится к кодированной температуре или эмпирической поправке,
а не заменяет закон Планка для отсчётов радиационной яркости.
"""
from __future__ import annotations
import csv
import io
import math
import numpy as np

K_UNITS = {'k', 'kelvin', 'kelvins'}
C_UNITS = {'c', '°c', 'celsius', 'degc', 'degree_celsius', 'degrees_celsius'}


def finite(value, name):
    if isinstance(value, bool):
        raise ValueError(name + ': нужно число, не логическое значение.')
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(name + ': укажите число.') from exc
    if not math.isfinite(result):
        raise ValueError(name + ': число должно быть конечным.')
    return result


def affine_values(scale, offset):
    a, b = finite(scale, 'Масштаб'), finite(offset, 'Сдвиг')
    if abs(a) < 1e-12 or abs(a) > 1e6 or abs(b) > 1e9:
        raise ValueError('Масштаб не может быть нулевым; проверьте диапазон коэффициентов.')
    return a, b


def metadata_scale(ds, bands=None):
    """Вернуть (a,b) в K, только когда единицы объявлены явно.

    TIFF и STAC проверяются независимо. При конфликте не выбираем удобный
    источник молча. Rasterio scale/offset имеют приоритет перед неформальными
    тегами только при отсутствии противоречия.
    """
    tag_fn = getattr(ds, 'tags', lambda *args: {})
    tags = {str(k).lower(): v for k, v in {**tag_fn(), **tag_fn(1)}.items()}
    unit = (ds.units[0] or tags.get('units') or tags.get('unit') or '').strip().lower()
    declarations = []
    if unit in K_UNITS | C_UNITS:
        a, b = affine_values(ds.scales[0], ds.offsets[0])
        if a <= 0:
            raise ValueError('Неположительный масштаб в метаданных GeoTIFF.')
        if 'scale_factor' in tags or 'add_offset' in tags:
            aa, bb = affine_values(tags.get('scale_factor', 1), tags.get('add_offset', 0))
            if (a, b) != (1., 0.) and not np.allclose([a, b], [aa, bb], rtol=1e-9, atol=1e-10):
                raise ValueError('В GeoTIFF противоречат друг другу scale/offset и scale_factor/add_offset.')
            a, b = aa, bb
        declarations.append((a, b + (273.15 if unit in C_UNITS else 0), 'GeoTIFF'))
    if isinstance(bands, list) and len(bands) == 1 and isinstance(bands[0], dict):
        band = bands[0]
        stac_unit = str(band.get('unit', band.get('units', ''))).strip().lower()
        if stac_unit in K_UNITS | C_UNITS:
            if unit and unit not in K_UNITS | C_UNITS:
                raise ValueError('Единицы GeoTIFF и STAC не согласованы. Автоматический перевод остановлен.')
            a, b = affine_values(band.get('scale', 1), band.get('offset', 0))
            declarations.append((a, b + (273.15 if stac_unit in C_UNITS else 0), 'STAC raster:bands'))
    if not declarations:
        return None
    if any(not np.allclose(declarations[0][:2], d[:2], rtol=1e-9, atol=1e-8) for d in declarations[1:]):
        raise ValueError('GeoTIFF и STAC задают разные температурные шкалы. Нужна проверка источника.')
    a, b, source = declarations[0]
    return dict(scale=a, offset=b, units='K', status='metadata', reference=source, method='explicit_metadata')


def two_points(dn1, t1, dn2, t2):
    x1, y1, x2, y2 = [finite(v, n) for v, n in zip((dn1, t1, dn2, t2), ('DN 1','T 1','DN 2','T 2'))]
    if abs(x2-x1) < 1e-9 or abs(y2-y1) < 1e-9:
        raise ValueError('Две опоры должны различаться и по DN, и по температуре.')
    if not 120 <= min(y1,y2) <= max(y1,y2) <= 400:
        raise ValueError('Опорные температуры: 120–400 K. Проверьте единицы.')
    a, b = affine_values((y2-y1)/(x2-x1), y1-(y2-y1)/(x2-x1)*x1)
    return dict(scale=a, offset=b, method='two_anchors', anchors=[[x1,y1],[x2,y2]],
                valid_dn=sorted([x1,x2]), n=2, validation='not_independently_validated')


def fit_pairs(text):
    """OLS по предварительно коллоцированным парам. RMSE — ошибка подгонки."""
    if not isinstance(text, str) or len(text) > 1_000_000:
        raise ValueError('CSV сопоставлений: не более 1 МБ.')
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')), delimiter=';' if ';' in text.split('\n')[0] else ',')
    names = reader.fieldnames or []
    temp = [name for name in ('temperature_k', 'temperature_c') if name in names]
    if 'dn' not in names or len(temp) != 1 or len(names) != len(set(names)):
        raise ValueError('CSV: dn и ровно один столбец temperature_k либо temperature_c. Можно добавить group.')
    if set(names) - {'dn','temperature_k','temperature_c','group'}:
        raise ValueError('Неизвестные столбцы CSV сопоставлений.')
    rows = list(reader)
    if not 6 <= len(rows) <= 10000:
        raise ValueError('Нужно 6–10000 сопоставленных пар, охватывающих разные температуры.')
    if any(None in r or any(v is None or not v.strip() for v in r.values()) for r in rows):
        raise ValueError('CSV содержит пустые или лишние поля.')
    x = np.array([finite(r['dn'].replace(',','.'), 'DN') for r in rows])
    y = np.array([finite(r[temp[0]].replace(',','.'), 'Температура') for r in rows])
    if temp[0] == 'temperature_c':
        y += 273.15
    if np.any((y < 120) | (y > 400)) or np.ptp(x) < 1e-6 or np.ptp(y) < 1:
        raise ValueError('Недостаточный диапазон опор либо неверные единицы температуры.')
    xc = x-x.mean()
    a = float(np.dot(xc, y-y.mean())/np.dot(xc, xc)); b = float(y.mean()-a*x.mean())
    affine_values(a, b)
    residual = a*x+b-y
    result = dict(scale=a, offset=b, method='matched_pairs', n=len(x),
        valid_dn=[float(x.min()), float(x.max())], rmse_fit_k=float(np.sqrt(np.mean(residual**2))),
        max_residual_k=float(np.max(np.abs(residual))), validation='fit_only_not_accuracy',
        plot=[[float(xx),float(yy)] for xx,yy in zip(x,y)][:300])
    groups = sorted(set(r.get('group','') for r in rows))
    if len(groups) >= 3:
        errors = []
        for group in groups:
            take = np.array([r.get('group') != group for r in rows])
            xx, yy = x[take], y[take]
            if len(xx) < 4 or np.ptp(xx) < 1e-6:
                continue
            ac = float(np.dot(xx-xx.mean(), yy-yy.mean())/np.sum((xx-xx.mean())**2))
            bc = float(yy.mean()-ac*xx.mean())
            errors.extend((ac*x[~take]+bc-y[~take]).tolist())
        if len(errors) == len(x):
            result.update(rmse_group_cv_k=float(np.sqrt(np.mean(np.square(errors)))), groups=len(groups))
    return result
