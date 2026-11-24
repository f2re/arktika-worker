"""Проверка исходного STAC для получения измерений и QC через GPTL.

Изображения, архивные даты и версии принимаются только по исходным полям.
Модуль описывает входы для загрузчика, проверка байтов выполняется отдельно.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from .model import normalize_asset
from .network import S3_HOSTS

_INSTRUMENTS = {'ARCM1': 'MSU-GS/A', 'ARCM2': 'MSU-GS/A',
                'EL2': 'MSU-GS', 'EL3': 'MSU-GS', 'EL4': 'MSU-GS'}
_TIMESTAMPS_EXTENSION = 'https://stac-extensions.github.io/timestamps/v1.1.0/schema.json'
_RFC3339 = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
                      r'(?:\.[0-9]{1,6})?(?:Z|[+-][0-2][0-9]:[0-5][0-9])')
_MAX_METADATA = 8 * 1024**2


def _object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('Повторяющийся ключ исходного STAC.')
        obj[key] = value
    return obj


def _reject_number(_):
    raise ValueError('Нечисловая константа в исходном STAC.')


def _timestamp(value, field, *, optional=False):
    if value is None and optional:
        return None
    if (not isinstance(value, str) or not _RFC3339.fullmatch(value)
            or value.endswith('-00:00')):
        raise ValueError(f'{field}: нужна исходная дата со смещением UTC.')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f'{field}: некорректная дата исходного STAC.') from exc


def _stamp(value):
    return value.isoformat().replace('+00:00', 'Z') if value is not None else None


def _asset_address(raw, historical):
    href = raw.get('href')
    if (not isinstance(href, str) or not href or not href.isascii()
            or any(ord(char) <= 32 or ord(char) >= 127 for char in href)):
        raise ValueError('В STAC отсутствует корректная ссылка на объект GPTL.')
    uri = urlsplit(href)
    path_parts = unquote(uri.path).split('/')
    if (uri.scheme != 'https' or uri.netloc not in S3_HOSTS
            or uri.fragment or len(path_parts) < 3 or not all(path_parts[1:])
            or any(part in ('.', '..') for part in path_parts)
            or any(ord(char) < 32 or ord(char) == 127 for char in unquote(uri.path))
            or not uri.path.lower().endswith(('.tif', '.tiff'))):
        raise ValueError('Ожидается прямой HTTPS-объект GeoTIFF в хранилище GPTL.')
    params = parse_qs(uri.query, keep_blank_values=True)
    if set(params) - {'versionId'}:
        raise ValueError('Нужна ссылка STAC без подписи или других параметров доступа.')
    versions = params.get('versionId', [])
    version = versions[0] if len(versions) == 1 else None
    if uri.query and (not version or version.strip() != version or version.lower() == 'null'
                      or any(ord(char) < 32 or ord(char) == 127 for char in version)):
        raise ValueError('Некорректная явная версия объекта GPTL.')
    if historical and (uri.netloc != 's3.gptl.ru' or not version):
        raise ValueError('Для архива нужна явная версия объекта s3.gptl.ru.')
    return href, (uri.netloc, unquote(uri.path), version)


def _source_hash(asset, historical):
    checksum = asset.get('file:checksum')
    if checksum is None and not historical:
        return None
    if not isinstance(checksum, str):
        raise ValueError('Для этой версии нужен SHA256 из file:checksum.')
    checksum = re.fullmatch(r'(?:1220|sha256:)?([0-9a-f]{64})', checksum)
    if not checksum:
        raise ValueError('В file:checksum требуется SHA256.')
    return checksum.group(1)


def _channel(raw, key, uri):
    values = []
    explicit = raw.get('channel')
    if explicit is not None:
        if type(explicit) is not int and not (
                isinstance(explicit, str) and re.fullmatch(r'[0-9]{1,2}', explicit)):
            raise ValueError('Некорректный номер канала в STAC.')
        values.append(int(explicit))
    key_match = re.search(r'(?:^ch|\.ir\.[0-9]+\.)([0-9]{1,2})$', key, re.IGNORECASE)
    name_match = re.search(r'_ch([0-9]{1,2})\.tiff?$', unquote(urlsplit(uri).path),
                           re.IGNORECASE)
    values.extend(int(match.group(1)) for match in (key_match, name_match) if match)
    if len(set(values)) > 1:
        raise ValueError('Номер ИК-канала в полях STAC противоречив.')
    return values[0] if values else None


def _calibration(raw, key, scene_id):
    bands = raw.get('raster:bands')
    if not isinstance(bands, list) or len(bands) != 1 or not isinstance(bands[0], dict):
        raise ValueError('Нужен один численный канал с raster:bands.')
    band = bands[0]
    if band.get('unit') != 'K':
        raise ValueError('В raster:bands требуется единица K.')
    for field in ('scale', 'offset', 'nodata'):
        value = band.get(field)
        if value is None and field == 'nodata':
            continue
        if type(value) not in (int, float) or abs(value) > 1e308 or not math.isfinite(value):
            raise ValueError(f'Некорректный коэффициент или маркер {field} в raster:bands.')
    if band['scale'] <= 0 or not math.isfinite(band['scale'] + band['offset']):
        raise ValueError('Требуются положительный scale и конечная сумма коэффициентов.')
    revision = raw.get('calibration:version')
    if revision is not None and (not isinstance(revision, str) or not revision.strip()):
        raise ValueError('Некорректная версия калибровки в STAC.')
    return {'asset_key': key, 'reference': f'STAC {scene_id}, {key}/raster:bands',
            'revision': revision, 'unit': 'K', 'scale': band['scale'], 'offset': band['offset']}


def _quality(raw, key, scene_id):
    roles = raw.get('roles')
    if not isinstance(roles, list) or not any(role in roles for role in ('quality', 'quality-mask')):
        raise ValueError('Нужен отдельный актив качества с ролью quality или quality-mask.')
    classes = raw.get('classification:classes')
    bands = raw.get('raster:bands')
    if raw.get('classification:bitfields') is not None:
        raise ValueError('Для QC ожидается легенда целых значений пикселей.')
    if bands is not None:
        if not isinstance(bands, list) or len(bands) != 1 or not isinstance(bands[0], dict):
            raise ValueError('Маска качества должна содержать один заявленный канал.')
        if bands[0].get('classification:bitfields') is not None:
            raise ValueError('Для QC ожидается легенда целых значений пикселей.')
        band_classes = bands[0].get('classification:classes')
        if band_classes is not None and classes is not None and band_classes != classes:
            raise ValueError('Расходятся легенды качества актива и канала QC.')
        if band_classes is not None:
            classes = band_classes
    if (not isinstance(classes, list) or not classes
            or any(not isinstance(entry, dict) or type(entry.get('value')) is not int
                   or not isinstance(entry.get('name'), str) for entry in classes)
            or len({entry['value'] for entry in classes}) != len(classes)):
        raise ValueError('В исходном STAC требуется однозначная легенда classification:classes для QC.')
    good = [row['value'] for row in classes
            if row['name'].casefold() in ('good', 'valid', 'good_data', 'valid_data')]
    if good != [0]:
        raise ValueError('Нужен явно подтверждённый исходной легендой флаг 0 для годного пикселя.')
    return {'asset_key': key, 'reference': f'STAC {scene_id}, {key}/classification:classes',
            'good_flag': 0, 'classes': classes}


def _publication(asset, scan_end, historical):
    published = _timestamp(asset.get('published'), 'published актива', optional=not historical)
    ready = _timestamp(asset.get('ready_at'), 'ready_at актива', optional=True)
    if ((ready is not None and ready < scan_end)
            or (published is not None and (published < scan_end or (ready is not None and ready > published)))):
        raise ValueError('Время публикации актива противоречит времени съёмки или готовности.')
    if historical:
        for field in ('created', 'updated'):
            stamp = _timestamp(asset.get(field), field, optional=True)
            if stamp is not None and stamp > published:
                raise ValueError('В опубликованной архивной версии указано более позднее изменение.')
        if any(asset.get(field) is not None for field in ('expires', 'unpublished')):
            raise ValueError('Историческая доступность изъятого актива не подтверждена.')
    return published


def _normalized_asset(raw, key, href, version, checksum, context, channel, category):
    size = raw.get('file:size')
    if size is not None and (type(size) is not int or size <= 0):
        raise ValueError('Некорректный file:size в исходном STAC.')
    # Берём проверенные href и время сцены. Дополнительные поля отдельных активов
    # не должны переопределять источник или ID в старом формате загрузчика.
    legacy = normalize_asset({'href': href, 'file:size': size, 'raster:bands': raw.get('raster:bands')},
                             context, key)
    original = urlsplit(href)
    canonical = urlunsplit((original.scheme, original.netloc, original.path, '', ''))
    legacy.update(id=sha256((context['id'] + '\n' + canonical).encode()).hexdigest()[:32],
                  channel=channel, category=category, provider_sha256=checksum, object_version=version,
                  time=context['time'], time_original=context['time'], time_assumed=False)
    return legacy


def parse_native_item(raw: bytes, raster_key: str, quality_key: str, *, historical=False) -> dict:
    """Возвращает план загрузки одного ИК-канала с маской. Исходник передаётся байтами."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_METADATA:
        raise ValueError('Нужен непустой исходный STAC JSON размером не более 8 МиБ.')
    item = json.loads(raw.decode('utf-8'), object_pairs_hook=_object, parse_constant=_reject_number)
    if not isinstance(item, dict) or item.get('type') != 'Feature':
        raise ValueError('Нужен отдельный исходный STAC Item.')
    scene_id, properties, assets = (item.get(key) for key in ('id', 'properties', 'assets'))
    if (not isinstance(scene_id, str) or not scene_id.strip()
            or not isinstance(properties, dict) or not isinstance(assets, dict)):
        raise ValueError('В STAC Item отсутствует идентификатор, свойства сцены или активы.')
    platform = properties.get('platform') or properties.get('platform_identifier')
    if not isinstance(platform, str) or platform not in _INSTRUMENTS:
        raise ValueError('Нужен явно указанный аппарат ARCM1/2 или EL2/3/4.')
    if any(properties.get(key) not in (None, platform) for key in ('platform', 'platform_identifier')):
        raise ValueError('Поля аппарата в исходном STAC противоречивы.')
    if (not isinstance(raster_key, str) or not raster_key or not isinstance(quality_key, str)
            or not quality_key or raster_key == quality_key):
        raise ValueError('Укажите разные точные ключи ИК-канала и QC.')
    raster, quality = (assets.get(key) for key in (raster_key, quality_key))
    if not isinstance(raster, dict) or not isinstance(quality, dict):
        raise ValueError('В STAC отсутствует запрошенный ИК-канал или его актив QC.')
    level = (raster.get('processing:level') or properties.get('processing:level')
             or properties.get('processing_level_code'))
    levels = [obj[key] for obj in (properties, raster)
              for key in ('processing:level', 'processing_level_code', 'level')
              if obj.get(key) is not None]
    if level != 'L2IR' or any(value != 'L2IR' for value in levels):
        raise ValueError('Нужен явно указанный численный продукт L2IR.')

    raster_uri, raster_identity = _asset_address(raster, historical)
    quality_uri, quality_identity = _asset_address(quality, historical)
    data_sha = _source_hash(raster, historical)
    qc_sha = _source_hash(quality, historical)
    if raster_identity[1] == quality_identity[1] or (data_sha is not None and data_sha == qc_sha):
        raise ValueError('Растр и QC должны быть разными объектами.')
    channel = _channel(raster, raster_key, raster_uri)
    if channel not in range(4, 11):
        raise ValueError('Нужен явно указанный номер физического ИК-канала 4–10.')
    qc_channel = _channel(quality, quality_key, quality_uri)
    if qc_channel is not None and qc_channel != channel:
        raise ValueError('Актив QC соответствует другому каналу.')
    if qc_channel is None:
        for candidate_key, candidate in assets.items():
            if candidate_key in (raster_key, quality_key) or not isinstance(candidate, dict):
                continue
            candidate_bands = candidate.get('raster:bands')
            if isinstance(candidate_bands, list) and any(
                    isinstance(band, dict) and band.get('unit') == 'K' for band in candidate_bands):
                raise ValueError('Для нескольких численных каналов требуется явная связь QC с ИК-каналом.')
    calibration = _calibration(raster, raster_key, scene_id)
    quality_definition = _quality(quality, quality_key, scene_id)

    start = _timestamp(properties.get('start_datetime'), 'start_datetime')
    end = _timestamp(properties.get('end_datetime'), 'end_datetime')
    observed = _timestamp(properties.get('datetime'), 'datetime')
    ready = _timestamp(properties.get('ready_at'), 'ready_at', optional=not historical)
    if not start < end or not start <= observed <= end or (ready is not None and ready < end):
        raise ValueError('Противоречивые границы съёмки, номинальное время или готовность поставщика.')
    for asset in (raster, quality):
        for field, expected in (('start_datetime', start), ('end_datetime', end)):
            if asset.get(field) is not None and _timestamp(asset[field], field) != expected:
                raise ValueError('Границы съёмки актива расходятся со сценой.')
    if historical:
        extensions = item.get('stac_extensions')
        if not isinstance(extensions, list) or _TIMESTAMPS_EXTENSION not in extensions:
            raise ValueError('Для архива нужен стандарт STAC Timestamps 1.1.0.')
    raster_published = _publication(raster, end, historical)
    quality_published = _publication(quality, end, historical)
    available = max(ready, raster_published, quality_published) if historical else None
    context = {'id': scene_id, 'platform': platform, 'time': _stamp(observed),
               'level': 'L2IR', 'source': 'gptl-native'}
    raster_asset = _normalized_asset(raster, raster_key, raster_uri, raster_identity[2],
                                     data_sha, context, channel, 'channel')
    quality_asset = _normalized_asset(quality, quality_key, quality_uri, quality_identity[2],
                                      qc_sha, context, channel, 'quality')
    return {'scene_id': scene_id, 'platform': platform, 'instrument': _INSTRUMENTS[platform],
            'channel': channel, 'observed_at': _stamp(observed), 'scan_start': _stamp(start),
            'scan_end': _stamp(end), 'provider_ready_at': _stamp(ready),
            'available_at': _stamp(available), 'raster_published_at': _stamp(raster_published),
            'quality_published_at': _stamp(quality_published), 'historical': historical,
            'producer_item_sha256': sha256(raw).hexdigest(), 'calibration': calibration,
            'quality': quality_definition, 'raster_asset': raster_asset, 'quality_asset': quality_asset}
