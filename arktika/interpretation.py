"""Объяснимые спектральные признаки и сопоставление с внешним профилем.

Пороговые правила — инженерные гипотезы, не валидированный классификатор МСУ-ГС/А.
Цвет картинки не участвует в классификации. Спутниковая вершина не переносится
на эшелон. Для слоя используются только коллоцированные профильные данные.
"""
from __future__ import annotations
import csv
import datetime as dt
import io
import json
import math
from typing import Any
import numpy as np
from pyproj import Geod
from .profiles import cloud_top, integrate

VERSION = 'spectral-rules-0.2.2'
SOURCES = {
    'microphysics': 'https://eumetrain.org/sites/default/files/2025-03/24_micro_QG.pdf',
    'night': 'https://resources.eumetrain.org/data/4/410/print_4.htm',
    'icing': 'https://www.weather.gov/source/zhu/ZHU_Training_Page/icing_stuff/icing/icing.htm',
    'profiles': 'https://www.star.nesdis.noaa.gov/goesr/product_profiles.php',
}
THRESHOLDS = dict(ice_max_k=253.15, ice_delta97_max_k=1.0,
                  thin_delta109_k=-2.0, water_delta97_min_k=2.0,
                  night_delta94_min_k=2.0, night_delta94_max_k=10.0,
                  cold_k=233.15, wv_ir_close_k=-3.0, mismatch_k=15.0,
                  liquid_floor_kg_kg=1e-6)

# Swatches are qualitative reference colours for the particular recipe, not classes
# calculated by matching screen RGB. Stretch and mixed pixels change the observed hue.
GUIDES = {
    'micro24': {
        'title': 'Фаза и прозрачность облачности',
        'purpose': 'Сопоставьте оттенок, оконную температуру и разности каналов.',
        'swatches': [
            dict(color='#aa243b', title='Красные тона',
                 meaning='Плотные холодные ледяные вершины — либо очень холодная поверхность.',
                 application='Для поиска наковален проверьте форму, охлаждение и рост облачного объекта. Одного красного цвета недостаточно для Cb.'),
            dict(color='#353451', title='Тёмно-синие и почти чёрные',
                 meaning='Возможны тонкие перистые облака над более тёплым фоном.',
                 application='Проверьте отрицательную T10−T9 и рисунок волокон. Температура полупрозрачного облака смешана с сигналом снизу.'),
            dict(color='#b6bb5e', title='Жёлто-зелёные / оливковые',
                 meaning='Часто водяные или смешанные облака; оттенок зависит от температуры и прозрачности.',
                 application='Ночью сравните с T9−T4. Для обледенения нужна жидкая вода и температура именно на выбранной высоте.'),
            dict(color='#427fc0', title='Синие / голубые',
                 meaning='Тёплый фон также даёт синий вклад; это не универсальный цвет льда.',
                 application='Сопоставьте с поверхностью и T9. Цвет зависит от всей композиции, а не от одной синей компоненты.'),
        ],
        'source': SOURCES['microphysics'],
    },
    'night': {
        'title': 'Низкие водяные облака ночью',
        'purpose': 'Зелёный вклад усиливается при положительной разности T9−T4.',
        'swatches': [
            dict(color='#bad69a', title='Зелёные и светло-бирюзовые',
                 meaning='Кандидаты низкой водяной облачности при ночной геометрии.',
                 application='Для тумана нужна связь облачного слоя с поверхностью: METAR, видимость, облакомер или профиль.'),
            dict(color='#8f2936', title='Красные и тёмно-красные',
                 meaning='Холодные плотные вершины с небольшим синим вкладом.',
                 application='Проверьте микрофизику 24 ч и динамику; верхняя ледяная облачность может скрывать нижний водяной слой.'),
            dict(color='#937db0', title='Сиреневые и розовые',
                 meaning='Возможны поверхность, тонкие облака и смешанный сигнал.',
                 application='Не определяйте фазу только по оттенку. За границей ночной маски продукт скрыт.'),
        ],
        'source': SOURCES['night'],
    },
    'dust': {
        'title': 'Пылевой спектральный контраст',
        'purpose': 'Сравните пылевой рисунок с облачностью и атмосферным переносом.',
        'swatches': [dict(color='#d57da9', title='Розовый контраст',
                         meaning='Один из характерных оттенков пыли в этой растяжке; возможны другие причины.',
                         application='Проверьте подстилающую поверхность, последовательность и независимые сведения об аэрозоле.')],
        'source': 'https://www.eumetrain.org/sites/default/files/2025-02/dust_QG.pdf',
    },
    'ash': {
        'title': 'Контраст пепла',
        'purpose': 'Проверьте разности оконных каналов и канал 8,7 мкм.',
        'swatches': [dict(color='#d9616e', title='Красно-розовый контраст',
                         meaning='Спектральный кандидат, не подтверждённая зона вулканического пепла.',
                         application='Сопоставьте с сообщением об извержении, направлением переноса и консультативной информацией VAAC.')],
        'source': 'https://www.jma.go.jp/jma/jma-eng/satellite/VLab/QG/RGB_QG_Ash_en.pdf',
    },
    'difference': {
        'title': 'Разность оконных каналов',
        'purpose': 'T10−T9: отрицательные значения часто усиливаются у полупрозрачных облаков, но зависят и от поверхности/влажности.',
        'swatches': [], 'source': SOURCES['microphysics'],
    },
    'channel': {
        'title': 'Температура излучения',
        'purpose': 'Оконная T9 помогает выделять холодные объекты; высоту уточняют по T(z) и признакам непрозрачности.',
        'swatches': [], 'source': SOURCES['microphysics'],
    },
    'indicators': {
        'title': 'Совместные спектральные признаки',
        'purpose': 'Отдельно показаны холодный сигнал, ночной водяной признак и контроль качества.',
        'swatches': [], 'source': SOURCES['microphysics'],
    },
}


PHASE_CLASSES = [
    dict(value=0, color='#000000', name='Нет данных'),
    dict(value=1, color='#956ad0', name='Ледяной спектральный кандидат'),
    dict(value=2, color='#3aa994', name='Водяной спектральный кандидат'),
    dict(value=3, color='#e7af4d', name='Холодный сигнал: уточнить структуру'),
    dict(value=4, color='#8192a6', name='Фаза не выделена'),
    dict(value=5, color='#ce596d', name='Несогласованность каналов'),
]
GUIDES['phase'] = dict(title='Спектральные кандидаты фазы',
    purpose='Три канала совместно: T9, T9−T7 и T10−T9. Цвета этой карты обозначают правила, а не оттенки RGB.',
    swatches=[], source=SOURCES['microphysics'])


def phase_field(channels, common):
    """Same core rules as point analysis, vectorized; not a cloud mask."""
    t7,t9,t10=(channels[i].astype('float32') for i in (7,9,10))
    common=np.asarray(common,dtype=bool)&np.isfinite(t7)&np.isfinite(t9)&np.isfinite(t10)
    d97,d109=t9-t7,t10-t9
    bad=common & (((t7<120)|(t7>400)|(t9<120)|(t9>400)|(t10<120)|(t10>400)) |
                  (np.abs(d109)>THRESHOLDS['mismatch_k']))
    field=np.zeros(t9.shape,dtype='uint8');valid=common & ~bad
    field[valid]=4
    field[valid & (t9<THRESHOLDS['cold_k'])]=3
    field[valid & (t9<THRESHOLDS['ice_max_k']) & (d97<=THRESHOLDS['ice_delta97_max_k'])]=1
    field[valid & (t9>=243.15) & (t9<285) & (d97>=THRESHOLDS['water_delta97_min_k']) & (d109>=-2)]=2
    field[bad]=5
    return field


def number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(name + ': ожидается число, не логическое значение.')
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(name + ': укажите число.') from exc
    if not math.isfinite(result):
        raise ValueError(name + ': число должно быть конечным.')
    return result


def utc(value: str) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Срок профиля должен содержать UTC: Z или смещение часового пояса.')
    return stamp.astimezone(dt.timezone.utc)


def _card(identity, title, icon, tone, evidence, meaning, action, kind='hypothesis'):
    return dict(id=identity, title=title, icon=icon, tone=tone, kind=kind,
                evidence=evidence, meaning=meaning, action=action)


def spectral(pixel: dict) -> dict:
    """Evaluate a source pixel. Null/uncalibrated/bad channels never enter rules."""
    channels = {int(c['channel']): c for c in pixel.get('channels', [])}
    raw = {i: c['value'] for i, c in channels.items() if c.get('value') is not None}
    values = {i: float(v) for i, v in raw.items()
              if 4<=i<=10 and channels[i].get('unit') == 'K' and math.isfinite(float(v))}
    bad = [i for i, v in values.items() if not 120 <= v <= 400]
    metrics = []
    for i in (9, 5, 4, 7, 10):
        if i in values:
            metrics.append(dict(id='t'+str(i), label='Tя '+str(i), value=values[i]-273.15,
                                units='°C', kelvin=values[i], kind='assumption' if channels[i].get('calibration')=='assumed' else 'measurement',
                                explanation='Яркостная температура канала '+str(i)+'.'))
    diffs = {}
    descriptions = {
        'd109': 'Прозрачность облака, поверхность и атмосферное влияние.',
        'd97': 'Спектральная чувствительность к фазе, с влиянием поверхности.',
        'd94': 'Ночной водяной признак; днём меняется из-за солнечного вклада.',
        'd59': 'Сопоставление водяной полосы и окна; не CAPE и не T500.',
    }
    for a, b, key in ((10, 9, 'd109'), (9, 7, 'd97'), (9, 4, 'd94'), (5, 9, 'd59')):
        if a in values and b in values:
            diffs[key] = values[a]-values[b]
            metrics.append(dict(id=key, label='T'+str(a)+' − T'+str(b), value=diffs[key],
                                units='K', kind='derived', explanation=descriptions[key]))
    mismatch = abs(diffs.get('d109', 0)) > THRESHOLDS['mismatch_k']
    cards = []
    result = dict(version=VERSION, metrics=metrics, cards=cards, phase='unknown',
                  phase_label='Фаза не определена', conditions=[], quality_ok=bool(values) and not bad and not mismatch,
                  thresholds=THRESHOLDS, source=SOURCES['microphysics'],
                  cloud_mask_available=False, validation_status='unvalidated_rules',
                  calibration='assumed' if any(c.get('calibration') == 'assumed' for c in channels.values())
                  else ('declared' if any(c.get('calibration')=='declared' for c in channels.values()) else 'metadata' if values else 'unknown'))
    if not values:
        cards.append(_card('calibration', 'Нужна температурная шкала', 'tune', 'neutral', [],
                           'Числа DN не участвуют в температурных и фазовых правилах.',
                           'Укажите калибровку каналов.', 'missing'))
        return result
    if bad or mismatch:
        evidence = (['Каналы вне 120…400 K: '+', '.join(map(str, bad))] if bad else [])
        if mismatch:
            evidence.append('T10−T9 = {:.1f} K; |разность| > 15 K'.format(diffs['d109']))
        cards.append(_card('quality', 'Проверьте согласованность каналов', 'warning', 'error', evidence,
                           'Фазовые гипотезы подавлены в этом пикселе.',
                           'Сравните соседние пиксели и исходники.', 'quality'))
        return result
    bt = values.get(9)
    d97, d109, d94, d59 = [diffs.get(k) for k in ('d97', 'd109', 'd94', 'd59')]
    sun = pixel.get('sun_elevation')
    night = sun is not None and float(sun) <= -6
    if bt is not None and d97 is not None and d109 is not None:
        evidence = ['Tя9 = {:.1f} °C'.format(bt-273.15),
                    'T9−T7 = {:.1f} K'.format(d97), 'T10−T9 = {:.1f} K'.format(d109)]
        if bt < THRESHOLDS['ice_max_k'] and d97 <= THRESHOLDS['ice_delta97_max_k']:
            thin = d109 < THRESHOLDS['thin_delta109_k']
            result.update(phase='ice_candidate', phase_label='Ледяной спектральный кандидат')
            cards.append(_card('ice', 'Тонкая ледяная облачность?' if thin else 'Холодная ледяная вершина?',
                               'snow', 'violet', evidence,
                               'Малый вклад T9−T7 совместим со льдом. '+
                               ('Прозрачность затрудняет оценку температуры и высоты вершины.' if thin else
                                'Холодная поверхность может давать похожий сигнал.'),
                               'Проверьте облачную структуру и поверхность. Лёд наверху не означает обледенение на эшелоне.'))
        elif 243.15 <= bt < 285 and d97 >= THRESHOLDS['water_delta97_min_k'] and d109 >= -2:
            result.update(phase='water_candidate', phase_label='Водяной спектральный кандидат')
            if night and d94 is not None and 2 < d94 < 10:
                evidence.append('Ночью T9−T4 = {:.1f} K'.format(d94))
            cards.append(_card('water', 'Холодный водяной спектральный кандидат' if bt < 273.15 else 'Водяной спектральный кандидат',
                               'water', 'teal', evidence,
                               'Повышенная T9−T7 совместима с жидкими каплями; поверхность и смешанная фаза не исключены.',
                               'Сопоставьте ночной канал и профиль. Температура и вода на высоте маршрута проверяются отдельно.'))
        else:
            cards.append(_card('phase', 'Спектральная фаза неоднозначна', 'cloud', 'neutral', evidence,
                               'Комбинация не удовлетворяет выбранным пороговым гипотезам.',
                               'Сравните обе микрофизические композиции и соседние сроки.'))
    else:
        needed = [str(i) for i in (7, 9, 10) if i not in values]
        cards.append(_card('phase_missing', 'Фаза: нужны дополнительные каналы', 'layers', 'neutral',
                           ['Нет калиброванных каналов: '+', '.join(needed)],
                           'Один оконный канал даёт температуру, но не однозначное фазовое состояние.',
                           'Откройте каналы 7, 9 и 10.', 'missing'))
    if bt is not None and d94 is not None and night and 2 < d94 < 10 and bt > 243:
        cards.append(_card('night_water', 'Ночной признак низкого водяного слоя', 'fog', 'teal',
                           ['T9−T4 = {:.1f} K'.format(d94), 'Солнце {:.1f}°'.format(sun)],
                           'Спектральный контраст поддерживает водяную облачность, но не определяет её контакт с землёй.',
                           'Для тумана проверьте METAR/видимость и нижнюю границу.'))
    if bt is not None and bt < THRESHOLDS['cold_k']:
        evidence = ['Tя9 = {:.1f} °C'.format(bt-273.15)]
        if d59 is not None:
            evidence.append('T5−T9 = {:.1f} K'.format(d59))
        support = d59 is not None and d59 >= THRESHOLDS['wv_ir_close_k']
        cards.append(_card('convection', 'Глубокая облачность: проверить развитие' if support else 'Холодный объект: проверьте структуру',
                           'storm', 'amber', evidence,
                           'Холодный сигнал'+(' и близость водяного канала к оконному' if support else '')+
                           ' — повод проверить высокую вершину. Тип Cb и активная конвекция не установлены.',
                           'Сравните охлаждение и рост объекта в серии; проверьте МРЛ и молнии.'))
    result['conditions'] = [c['id'] for c in cards if c['kind'] == 'hypothesis']
    return result


def normalize_profile(data: dict) -> dict:
    """Explicit units and provenance; never infer real atmospheric values."""
    if not isinstance(data, dict):
        raise ValueError('Профиль должен быть JSON-объектом.')
    if not isinstance(data.get('source'),str) or not data['source'].strip():
        raise ValueError('Укажите источник профиля.')
    stamp = utc(data.get('valid_time', ''))
    lat, lon = number(data.get('lat'), 'Широта'), number(data.get('lon'), 'Долгота')
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Некорректные координаты профиля.')
    if data.get('height_units') != 'm' or data.get('temperature_units') not in ('K', 'C'):
        raise ValueError('Нужны height_units=m и temperature_units=K или C.')
    z = np.asarray(data.get('height', []), dtype=float)
    t = np.asarray(data.get('temperature', []), dtype=float)
    if z.ndim != 1 or not 2 <= len(z) <= 4096 or t.shape != z.shape:
        raise ValueError('Высота и температура: 2–4096 совпадающих уровней.')
    if not np.isfinite(z).all() or not np.isfinite(t).all() or not np.all(np.diff(z) > 0):
        raise ValueError('Уровни высоты должны возрастать без пропусков и повторений.')
    if data['temperature_units'] == 'C':
        t = t+273.15
    if np.any(z < -500) or np.any(z > 35000) or np.any(t < 120) or np.any(t > 340):
        raise ValueError('Проверьте единицы высоты и температуры.')
    radius = number(data.get('radius_km', 100), 'Радиус')
    hours = number(data.get('max_hours', 3), 'Допуск времени')
    if not 1 <= radius <= 500 or not .1 <= hours <= 12:
        raise ValueError('Радиус 1…500 км, допуск времени 0,1…12 ч.')
    out = dict(source=str(data['source']).strip()[:300], valid_time=stamp.isoformat().replace('+00:00', 'Z'),
               lat=lat, lon=lon, radius_km=radius, max_hours=hours,
               height=z.tolist(), temperature=t.tolist(), height_units='m', temperature_units='K',
               vertical_reference='AMSL', note='Высота над средним уровнем моря; точечный профиль.')
    if data.get('vertical_reference', 'AMSL') != 'AMSL':
        raise ValueError('Поддерживается геометрическая высота AMSL, не эшелон и не AGL.')
    specs = [('pressure', 'pressure_units', ('Pa','hPa'), 110000),
             ('specific_humidity', 'humidity_units', ('kg/kg',), .1),
             ('cloud_liquid', 'condensate_units', ('kg/kg',), .1),
             ('cloud_ice', 'condensate_units', ('kg/kg',), .1),
             ('u', 'wind_units', ('m/s',), 200), ('v', 'wind_units', ('m/s',), 200)]
    for key, unit_key, allowed, high in specs:
        if key not in data:
            continue
        if data.get(unit_key) not in allowed:
            raise ValueError(key+': укажите '+unit_key+'='+allowed[0])
        a = np.asarray(data[key], float)
        if key == 'pressure' and data[unit_key] == 'hPa':
            a *= 100
        if a.shape != z.shape or not np.isfinite(a).all() or np.any(abs(a) > high):
            raise ValueError(key+': неверные уровни, пропуски или диапазон.')
        if key not in ('u','v') and np.any(a < 0):
            raise ValueError(key+': отрицательные значения недопустимы.')
        if key == 'pressure' and (np.any(a <= 0) or not np.all(np.diff(a) < 0)):
            raise ValueError('Давление должно убывать с высотой.')
        out[key] = a.tolist()
        out[unit_key] = allowed[0]
    if ('u' in out) != ('v' in out):
        raise ValueError('Нужны обе компоненты ветра u и v.')
    return out


def parse_profile(data: dict) -> dict:
    """JSON or CSV. CSV schema names carry explicit units; metadata supplied separately."""
    if 'text' not in data:
        return normalize_profile(data)
    text = str(data['text']).lstrip('\ufeff')
    if len(text) > 1_000_000:
        raise ValueError('Профиль больше 1 МБ.')
    if text.lstrip().startswith('{'):
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError('Нужен объект JSON.')
    else:
        first = text.splitlines()[0] if text.splitlines() else ''
        delimiter = ';' if ';' in first else ','
        reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')), delimiter=delimiter)
        names = reader.fieldnames or []
        if len(names)!=len(set(names)) or any(not n or n.strip()!=n for n in names):
            raise ValueError('CSV: повторяющиеся, пустые заголовки или пробелы в именах столбцов.')
        if 'height_m' not in names or not any(n in names for n in ('temperature_c','temperature_k')):
            raise ValueError('CSV: нужны height_m и temperature_c либо temperature_k.')
        if 'temperature_c' in names and 'temperature_k' in names:
            raise ValueError('Не задавайте температуру в двух единицах одновременно.')
        rows = list(reader)
        if any(None in row or any(v is None or not v.strip() for v in row.values()) for row in rows):
            raise ValueError('CSV: число полей не совпадает с заголовком или есть пропуски.')
        if not 2 <= len(rows) <= 4096:
            raise ValueError('CSV: 2–4096 уровней.')
        def col(name):
            return [number(str(r[name]).replace(',', '.') if delimiter == ';' else r[name], name) for r in rows]
        result = dict(height=col('height_m'), height_units='m',
                      temperature=col('temperature_k' if 'temperature_k' in names else 'temperature_c'),
                      temperature_units='K' if 'temperature_k' in names else 'C')
        for column, key, unit_key, unit in [('pressure_hpa','pressure','pressure_units','hPa'),
                                           ('pressure_pa','pressure','pressure_units','Pa'),
                                           ('specific_humidity_kg_kg','specific_humidity','humidity_units','kg/kg'),
                                           ('cloud_liquid_kg_kg','cloud_liquid','condensate_units','kg/kg'),
                                           ('cloud_ice_kg_kg','cloud_ice','condensate_units','kg/kg'),
                                           ('u_ms','u','wind_units','m/s'),('v_ms','v','wind_units','m/s')]:
            if column in names:
                if key in result:
                    raise ValueError('Не задавайте '+key+' в двух единицах одновременно.')
                result[key] = col(column);result[unit_key] = unit
    for k, v in data.get('metadata', {}).items():
        if k in ('source','valid_time','lat','lon','radius_km','max_hours') and v not in (None, ''):
            result[k] = v
    return normalize_profile(result)


def applicability(profile: dict, lon: float, lat: float, time: str) -> dict:
    distance = abs(Geod(ellps='WGS84').inv(lon, lat, profile['lon'], profile['lat'])[2])/1000
    age = abs((utc(time)-utc(profile['valid_time'])).total_seconds())/3600
    reasons = []
    if distance > profile['radius_km']:
        reasons.append('Точка за радиусом профиля: {:.0f} км'.format(distance))
    if age > profile['max_hours']:
        reasons.append('Срок за допуском профиля: {:.1f} ч'.format(age))
    return dict(applicable=not reasons, reasons=reasons, distance_km=distance, time_gap_hours=age,
                source=profile['source'], valid_time=profile['valid_time'],
                radius_km=profile['radius_km'], max_hours=profile['max_hours'])


def height_ranges(z, t, bt, delta):
    """All exact linear intervals compatible with |T(z)-BT| <= delta, not a CI."""
    intervals = []
    for i in range(len(z)-1):
        if t[i] == t[i+1]:
            if abs(t[i]-bt) <= delta:
                intervals.append([float(z[i]), float(z[i+1])])
            continue
        aa, bb = sorted(((bt-delta-t[i])/(t[i+1]-t[i]), (bt+delta-t[i])/(t[i+1]-t[i])))
        a, b = max(0., aa), min(1., bb)
        if a <= b:
            intervals.append([float(z[i]+a*(z[i+1]-z[i])), float(z[i]+b*(z[i+1]-z[i]))])
    merged = []
    for a, b in intervals:
        if merged and a <= merged[-1][1]+1e-6:
            merged[-1][1] = max(b, merged[-1][1])
        else:
            merged.append([a,b])
    return merged


def profile_diagnostics(profile, pixel, analysis, altitude_m, cloud_confirmed=False,
                        opaque_confirmed=False, delta_k=2.) -> dict:
    use = applicability(profile, pixel['lon'], pixel['lat'], pixel['time'])
    result = dict(profile=use, height=dict(status='needs_cloud', title='Уточните облачный пиксель'),
                  layer=dict(status='missing', title='Высота не выбрана'))
    if not use['applicable']:
        result['height'] = dict(status='outside', title='Профиль не относится к этой точке')
        result['layer'] = dict(status='outside', title='Профиль вне допуска')
        return result
    indices=np.unique(np.linspace(0,len(profile['height'])-1,min(128,len(profile['height']))).astype(int))
    result['profile_plot']=dict(height_m=[profile['height'][i] for i in indices],temperature_c=[profile['temperature'][i]-273.15 for i in indices])
    values = {c['channel']:c['value'] for c in pixel['channels'] if c['unit']=='K' and c['value'] is not None}
    if cloud_confirmed is True and opaque_confirmed is True:
        delta = number(delta_k, 'Допуск температуры')
        if not .5 <= delta <= 10:
            raise ValueError('Допуск температуры: 0,5…10 K.')
        if not analysis['quality_ok'] or 9 not in values:
            result['height'] = dict(status='quality', title='Нет пригодной оконной температуры')
        elif 10 not in values or 7 not in values:
            result['height'] = dict(status='missing', title='Для спектрального контроля нужны 7, 9, 10')
        elif values[10]-values[9] < -2:
            result['height'] = dict(status='thin', title='Признак прозрачности: простая ВГО не рассчитывается')
        else:
            match = cloud_top(dict(mode='cloud_top', cloud_confirmed=True, height_units='m',
                                   temperature_units='K', height=profile['height'],
                                   temperature=profile['temperature'], brightness_temperature=values[9]))
            ranges = height_ranges(profile['height'],profile['temperature'],values[9],delta)
            result['height'] = dict(match, status=('no_solution' if not match['has_solution'] else 'ambiguous' if match['ambiguous'] else 'estimated'),
                                    title=('Нет соответствия в пределах профиля' if not match['has_solution'] else 'Высота по T(z)'), sensitivity_ranges_m=ranges,
                                    delta_k=delta, method='Tя9 = Tвершины = T(z)',
                                    note='AMSL; диапазон чувствительности к ±ΔT, не доверительный интервал. '
                                         'Облачность и непрозрачность подтверждены оператором в этой точке.')
    elif cloud_confirmed:
        result['height'] = dict(status='needs_opacity', title='Подтвердите непрозрачность облака')
    if altitude_m is not None:
        z = number(altitude_m, 'Высота')
        zz, tt = profile['height'], profile['temperature']
        if not zz[0] <= z <= zz[-1]:
            result['layer'] = dict(status='outside_height', title='Высота вне профиля', altitude_m=z)
        else:
            tk = float(np.interp(z,zz,tt)); c = tk-273.15
            layer = dict(altitude_m=z, temperature_c=c, temperature_k=tk,
                         status='missing_water', title='Нет данных о жидкой воде в слое',
                         icing_conditions=None, source=profile['source'], assessment_type='profile_thermodynamics',
                         droplet_size_available=False,
                         note='Условия по профилю; фаза спутниковой вершины на этот слой не переносится.')
            if 'cloud_liquid' in profile:
                ql = float(np.interp(z,zz,profile['cloud_liquid']))
                layer['cloud_liquid_kg_kg'] = ql
                layer['liquid_floor_kg_kg'] = THRESHOLDS['liquid_floor_kg_kg']
                liquid = ql > THRESHOLDS['liquid_floor_kg_kg']
                if liquid and -40 < c < 0:
                    layer.update(status='supercooled', title='В профиле — переохлаждённая жидкая вода', icing_conditions=True)
                elif liquid and c <= -40:
                    layer.update(status='inconsistent', title='Проверьте конденсат при T ≤ −40 °C', icing_conditions=None)
                elif c >= 0:
                    layer.update(status='warm', title='В слое T ≥ 0 °C', icing_conditions=None)
                else:
                    layer.update(status='not_resolved', title='Жидкая вода ниже заданного порога профиля', icing_conditions=None)
                if 'pressure' in profile:
                    pp = float(np.interp(z,zz,profile['pressure']))
                    q = float(np.interp(z,zz,profile['specific_humidity'])) if 'specific_humidity' in profile else 0
                    rho = pp/(287.05*tk*(1+.608*q))
                    layer.update(pressure_hpa=pp/100, liquid_water_g_m3=1000*rho*ql,
                                 density_method='Виртуальная температура' if 'specific_humidity' in profile else 'Приближение сухого воздуха')
            result['layer'] = layer
    if 'pressure' in profile and 'specific_humidity' in profile:
        result['column'] = integrate(profile)
    return result
