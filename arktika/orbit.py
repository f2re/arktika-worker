"""Расчёт параметров орбиты и геометрии наблюдения аппаратов Арктика-М (SGP4/TLE)."""
from __future__ import annotations
import json
import math
from datetime import datetime, timezone
import os
from pathlib import Path
import urllib.request
import urllib.error
from sgp4.api import Satrec, jday

NORAD_CATALOG = {
    'ARCM1': {'norad_id': 47719, 'name': 'ARKTIKA-M 1', 'cospar': '2021-016A'},
    'ARCM2': {'norad_id': 58584, 'name': 'ARKTIKA-M 2', 'cospar': '2023-198A'},
}

# Опорные TLE на случай отсутствия связи или недоступности открытых каталогов
BASELINE_TLES = {
    47719: (
        '1 47719U 21016A   26206.87236863 -.00000386  00000-0  00000-0 0  9995',
        '2 47719  63.2598  51.9767 7301002 270.1217  14.4714  2.00607275 39564'
    ),
    58584: (
        '1 58584U 23198A   26230.07438780 .00000056  00000-0  00000-0 0  9994',
        '2 58584  63.2129 147.3319 6901614 267.6403  18.7125  2.0059334319562'
    )
}


def parse_tle_text(text: str) -> tuple[str, str] | None:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    l1, l2 = None, None
    for line in lines:
        if line.startswith('1 ') and len(line) >= 68:
            l1 = line
        elif line.startswith('2 ') and len(line) >= 68:
            l2 = line
    if l1 and l2:
        return (l1, l2)
    return None


def fetch_tle(norad_id: int, cache_dir: Path | None = None) -> tuple[tuple[str, str], str]:
    """Получение TLE: CelesTrak -> SatNOGS -> локальный кэш -> встроенный опорный TLE."""
    cache_file = (cache_dir / f'{norad_id}.json') if cache_dir else None
    cached = None
    if cache_file and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass

    # 1. Попытка запроса к CelesTrak
    celestrak_url = f'https://celestrak.org/NORAD/elements/gp.php?CATNR={norad_id}&FORMAT=TLE'
    try:
        req = urllib.request.Request(celestrak_url, headers={'User-Agent': 'Arktika-M-Workstation/0.3'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode('utf-8', errors='ignore')
            pair = parse_tle_text(content)
            if pair:
                if cache_file:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(json.dumps({'tle': pair, 'source': 'celestrak.org', 'time': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')
                return pair, 'celestrak.org'
    except Exception:
        pass

    # 2. Попытка запроса к SatNOGS DB
    satnogs_url = f'https://db.satnogs.org/api/tle/?norad_cat_id={norad_id}'
    try:
        req = urllib.request.Request(satnogs_url, headers={'User-Agent': 'Arktika-M-Workstation/0.3'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
            if isinstance(data, list) and data:
                l1 = data[0].get('tle1')
                l2 = data[0].get('tle2')
                if l1 and l2:
                    pair = (l1.strip(), l2.strip())
                    if cache_file:
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        cache_file.write_text(json.dumps({'tle': pair, 'source': 'db.satnogs.org', 'time': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')
                    return pair, 'db.satnogs.org'
    except Exception:
        pass

    # 3. Локальный кэш
    if cached and 'tle' in cached:
        return tuple(cached['tle']), f"кэш ({cached.get('source', 'локальный')})"

    # 4. Встроенный опорный TLE
    if norad_id in BASELINE_TLES:
        return BASELINE_TLES[norad_id], 'базовый TLE каталога'

    raise ValueError(f'Не удалось получить TLE для спутника NORAD {norad_id}')


def calculate_viewing_geometry(platform: str, when: datetime, target_lat: float, target_lon: float, cache_dir: Path | None = None) -> dict:
    """Расчёт топоцентрической геометрии наблюдения (зенитный угол, азимут, дальность) через SGP4."""
    norm_plat = platform.upper().replace('-', '').replace('_', '')
    if 'ARCM2' in norm_plat or 'АРКТИКАМ2' in norm_plat:
        key = 'ARCM2'
    elif 'ARCM1' in norm_plat or 'АРКТИКАМ1' in norm_plat:
        key = 'ARCM1'
    else:
        raise ValueError(f'Неизвестная спутниковая платформа: {platform}')

    info = NORAD_CATALOG[key]
    pair, source = fetch_tle(info['norad_id'], cache_dir)
    line1, line2 = pair

    sat = Satrec.twoline2rv(line1, line2)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when_utc = when.astimezone(timezone.utc)

    jd, fr = jday(when_utc.year, when_utc.month, when_utc.day,
                  when_utc.hour, when_utc.minute, when_utc.second + when_utc.microsecond * 1e-6)

    err, r_teme, v_teme = sat.sgp4(jd, fr)
    if err != 0:
        raise ValueError(f'Ошибка SGP4 экстраполяции орбиты: код {err}')

    # Преобразование координат TEME (ECI) в ECEF через GMST
    t_ut1 = (jd + fr - 2451545.0) / 36525.0
    gmst_sec = 24110.54841 + 8640184.812866 * t_ut1 + 0.093104 * t_ut1**2 - 6.2e-6 * t_ut1**3
    gmst_rad = (gmst_sec % 86400.0) * (2 * math.pi / 86400.0)

    x_teme, y_teme, z_teme = r_teme
    x_ecef = math.cos(gmst_rad) * x_teme + math.sin(gmst_rad) * y_teme
    y_ecef = -math.sin(gmst_rad) * x_teme + math.cos(gmst_rad) * y_teme
    z_ecef = z_teme

    # Подспутниковая точка и высота
    sat_r = math.sqrt(x_ecef**2 + y_ecef**2 + z_ecef**2)
    sat_lat = math.degrees(math.asin(max(-1.0, min(1.0, z_ecef / sat_r))))
    sat_lon = math.degrees(math.atan2(y_ecef, x_ecef))
    sat_alt = sat_r - 6378.137

    # Координаты целевой точки на эллипсоиде WGS84
    lat_rad = math.radians(target_lat)
    lon_rad = math.radians(target_lon)
    a = 6378.137
    f = 1 / 298.257223563
    e2 = f * (2 - f)
    N = a / math.sqrt(1 - e2 * math.sin(lat_rad)**2)

    tx = N * math.cos(lat_rad) * math.cos(lon_rad)
    ty = N * math.cos(lat_rad) * math.sin(lon_rad)
    tz = (N * (1 - e2)) * math.sin(lat_rad)

    # Вектор от наземной точки к спутнику
    dx = x_ecef - tx
    dy = y_ecef - ty
    dz = z_ecef - tz
    dist = math.sqrt(dx**2 + dy**2 + dz**2)

    # Базис топоцентрической системы ENU (East, North, Up)
    up_x = math.cos(lat_rad) * math.cos(lon_rad)
    up_y = math.cos(lat_rad) * math.sin(lon_rad)
    up_z = math.sin(lat_rad)

    east_x = -math.sin(lon_rad)
    east_y = math.cos(lon_rad)
    east_z = 0.0

    north_x = -math.sin(lat_rad) * math.cos(lon_rad)
    north_y = -math.sin(lat_rad) * math.sin(lon_rad)
    north_z = math.cos(lat_rad)

    e_dist = dx * east_x + dy * east_y + dz * east_z
    n_dist = dx * north_x + dy * north_y + dz * north_z
    u_dist = dx * up_x + dy * up_y + dz * up_z

    cos_zenith = u_dist / dist
    zenith_deg = math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))
    azimuth_deg = (math.degrees(math.atan2(e_dist, n_dist)) + 360.0) % 360.0

    return {
        'platform': key,
        'norad_id': info['norad_id'],
        'satellite_name': info['name'],
        'tle_source': source,
        'time': when_utc.isoformat(),
        'target': {'lat': round(target_lat, 2), 'lon': round(target_lon, 2)},
        'zenith_deg': round(zenith_deg, 1),
        'azimuth_deg': round(azimuth_deg, 1),
        'distance_km': round(dist, 1),
        'subpoint': {
            'lat': round(sat_lat, 2),
            'lon': round(sat_lon, 2),
            'alt_km': round(sat_alt, 1),
        }
    }
