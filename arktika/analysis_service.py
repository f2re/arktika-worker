"""Persistence and API boundary for the scientific point inspector."""
from __future__ import annotations
import hashlib
import json
import re
import math
from pathlib import Path
from .download import atomic_json
from .interpretation import parse_profile, spectral, profile_diagnostics, applicability, VERSION


class AnalysisMixin:
    def _analysis_root(self, name):
        root = self.store.root/name
        root.mkdir(exist_ok=True)
        return root

    def import_profile(self, data):
        profile = parse_profile(data)
        profile['id'] = hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()[:24]
        atomic_json(self._analysis_root('profiles')/(profile['id']+'.json'), profile)
        return profile

    def profiles(self):
        result = []
        for path in self._analysis_root('profiles').glob('*.json'):
            if not re.fullmatch('[0-9a-f]{24}', path.stem):
                continue
            data = json.loads(path.read_text(encoding='utf-8'))
            result.append({k:data[k] for k in ('id','source','valid_time','lat','lon','radius_km','max_hours')})
        return sorted(result,key=lambda r:r['valid_time'],reverse=True)

    def get_profile(self, identity):
        if not re.fullmatch('[0-9a-f]{24}', str(identity)):
            raise ValueError('Выберите профиль.')
        path = self._analysis_root('profiles')/(identity+'.json')
        if not path.is_file():
            raise ValueError('Профиль не найден.')
        return json.loads(path.read_text(encoding='utf-8'))

    def _extract_era5_profile(self, nc_path, lat, lon, time_str):
        try:
            import xarray as xr
            import numpy as np
            with xr.open_dataset(nc_path) as ds:
                ds_lons = ds.longitude.values
                target_lon = (lon % 360) if ds_lons.min() >= 0 else lon
                pt = ds.sel(latitude=lat, longitude=target_lon, method='nearest').squeeze()
                z = np.asarray(pt.z, float) / 9.80665
                t = np.asarray(pt.t, float)
                p_hpa = np.asarray(pt.pressure_level, float)
                valid = np.isfinite(z) & np.isfinite(t) & (t > 120) & (t < 340) & (z >= -500) & (z <= 35000)
                z, t, p_hpa = z[valid], t[valid], p_hpa[valid]
                idx = np.argsort(z)
                z, t, p_hpa = z[idx], t[idx], p_hpa[idx]
                p_pa = p_hpa * 100
                good = [0]
                for i in range(1, len(z)):
                    if z[i] > z[good[-1]] and p_pa[i] < p_pa[good[-1]]:
                        good.append(i)
                z, t, p_pa = z[good], t[good], p_pa[good]
                if len(z) < 2:
                    return None
                time_iso = time_str if 'Z' in time_str else time_str + 'Z'
                actual_lon = float(pt.longitude if pt.longitude <= 180 else pt.longitude - 360)
                prof_data = {
                    'source': f"ERA5 Reanalysis (ECMWF, {time_iso[:16].replace('T', ' ')} UTC)",
                    'data_kind': 'era5',
                    'valid_time': time_iso,
                    'lat': float(pt.latitude),
                    'lon': actual_lon,
                    'radius_km': 500.0,
                    'max_hours': 12.0,
                    'height': z.tolist(),
                    'temperature': t.tolist(),
                    'height_units': 'm',
                    'temperature_units': 'K',
                    'vertical_reference': 'AMSL',
                    'pressure': p_pa.tolist(),
                    'pressure_units': 'Pa',
                    'note': 'Автоматический профиль реанализа ERA5 (ECMWF) из проверенного кэша.'
                }
                if 'q' in pt.data_vars:
                    q = np.asarray(pt.q, float)[valid][idx][good]
                    if np.all(np.isfinite(q)) and np.all(q >= 0):
                        prof_data['specific_humidity'] = q.tolist()
                        prof_data['humidity_units'] = 'kg/kg'
                return parse_profile(prof_data)
        except Exception:
            return None

    def _standard_arctic_profile(self, lat, lon, time_str):
        heights = [0.0, 111.0, 540.0, 762.0, 988.0, 1457.0, 1949.0, 2466.0, 3012.0, 3591.0, 4206.0,
                   4865.0, 5574.0, 6344.0, 7185.0, 8117.0, 9164.0, 10363.0, 11784.0, 12500.0,
                   13608.0, 16180.0, 18440.0, 20580.0, 23850.0, 26400.0, 31100.0]
        pressures = [101325.0, 100000.0, 95000.0, 92500.0, 90000.0, 85000.0, 80000.0, 75000.0, 70000.0,
                     65000.0, 60000.0, 55000.0, 50000.0, 45000.0, 40000.0, 35000.0, 30000.0,
                     25000.0, 20000.0, 17500.0, 15000.0, 10000.0, 7000.0, 5000.0, 3000.0, 2000.0, 1000.0]
        temps = [288.15, 287.4, 284.6, 283.2, 281.7, 278.7, 275.5, 272.1, 268.6, 264.8, 260.8,
                 256.5, 251.9, 246.9, 241.4, 235.4, 228.0, 220.2, 213.5, 213.15, 213.8,
                 216.7, 218.0, 220.0, 223.5, 226.5, 231.0]
        time_iso = time_str if 'Z' in time_str else time_str + 'Z'
        prof_data = {
            'source': 'Демонстрационный синтетический профиль (ICAO/COESA)',
            'valid_time': time_iso,
            'lat': float(lat),
            'lon': float(lon),
            'radius_km': 500.0,
            'max_hours': 12.0,
            'height': heights,
            'temperature': temps,
            'pressure': pressures,
            'height_units': 'm',
            'temperature_units': 'K',
            'pressure_units': 'Pa',
            'vertical_reference': 'AMSL',
            'note': 'Синтетическая иллюстрация для демонстрации интерфейса. Не является наблюдением или данными модели и не предназначена для метеорологической диагностики.',
            'data_kind': 'synthetic_demo'
        }
        profile = parse_profile(prof_data)
        profile['data_kind'] = 'synthetic_demo'
        profile['note'] = prof_data['note']
        return profile

    def resolve_auto_profile(self, lat, lon, time_str, demonstration=False):
        for p_meta in self.profiles():
            try:
                p = self.get_profile(p_meta['id'])
                app = applicability(p, lon, lat, time_str)
                if app['applicable']:
                    return p
            except Exception:
                continue

        cache_dir = self.store.root / 'era5' / 'cache'
        if cache_dir.is_dir():
            best_prof = None
            best_dist = float('inf')
            for meta_path in cache_dir.glob('*.json'):
                try:
                    meta = json.loads(meta_path.read_text(encoding='utf-8'))
                    nc_path = meta_path.with_suffix('.nc')
                    if not nc_path.is_file():
                        continue
                    item = meta.get('item', {})
                    if item.get('group') != 'pressure' or item.get('dataset') != 'reanalysis-era5-pressure-levels':
                        continue
                    area = item.get('request', {}).get('area')
                    if not area or len(area) != 4:
                        continue
                    north, west, south, east = float(area[0]), float(area[1]), float(area[2]), float(area[3])
                    c_lat = (north + south) / 2.0
                    c_lon = (west + east) / 2.0
                    dist = math.hypot(lat - c_lat, (lon - c_lon) * math.cos(math.radians(lat))) * 111.0
                    if dist < best_dist:
                        prof = self._extract_era5_profile(nc_path, lat, lon, item.get('time', time_str))
                        if prof:
                            best_prof = prof
                            best_dist = dist
                            if dist < 300:
                                break
                except Exception:
                    continue
            if best_prof and best_dist < 1500:
                best_prof['id'] = hashlib.sha256(json.dumps(best_prof, sort_keys=True).encode()).hexdigest()[:24]
                return best_prof

        if demonstration is True:
            prof = self._standard_arctic_profile(lat, lon, time_str)
            prof['id'] = hashlib.sha256(json.dumps(prof, sort_keys=True).encode()).hexdigest()[:24]
            return prof
        return None

    def analyse(self, data):
        product = self.product(data['product'])
        scene = self.frozen_scene(product)
        snapshot = self.sample_provenance(scene)
        pixel = self.pixel(data)
        analysis = spectral(pixel)
        product = self.product(data['product'])
        result = dict(pixel=pixel, analysis=analysis, product_id=product['id'],
                      method_version=VERSION, assumptions=dict(cloud_confirmed=data.get('cloud_confirmed') is True,
                                                               opaque_confirmed=data.get('opaque_confirmed') is True))
        profile = None
        is_auto = False
        if data.get('profile_id'):
            profile = self.get_profile(data['profile_id'])
        else:
            try:
                profile = self.resolve_auto_profile(pixel['lat'], pixel['lon'], pixel['time'],
                                                    demonstration=data.get('demo_profile') is True)
                is_auto = True
            except Exception:
                profile = None

        if profile:
            result['profile_result'] = profile_diagnostics(profile, pixel, analysis,
                    data.get('altitude_m'), data.get('cloud_confirmed') is True,
                    data.get('opaque_confirmed') is True, data.get('delta_k', 2))
            result['profile_id'] = profile['id']
            result['profile_snapshot'] = profile
            result['profile_auto'] = is_auto
            result['profile_kind'] = profile.get('data_kind') or 'provided_profile'
        # A complete reproducible record, not just a rendered colour.
        scene = self.frozen_scene(product)
        result['inputs'] = self.sample_provenance(scene)
        if snapshot != result['inputs']:
            raise ValueError('Исходник изменился во время выборки точки. Повторите анализ.')
        identity = hashlib.sha256(json.dumps(result,sort_keys=True,allow_nan=False).encode()).hexdigest()[:24]
        result['id'] = identity
        atomic_json(self._analysis_root('analyses')/(identity+'.json'), result)
        return result

    def analysis_export(self, identity):
        if not re.fullmatch('[0-9a-f]{24}',str(identity)):
            raise ValueError('Неверный идентификатор анализа.')
        path = self._analysis_root('analyses')/(identity+'.json')
        if not path.is_file():
            raise ValueError('Анализ не найден.')
        return path.read_bytes()

    def sample_provenance(self, scene):
        from .download import digest
        rows = []
        for key, entry in scene['channels'].items():
            path = Path(entry['path'])
            before = path.stat()
            sha = digest(path)
            after = path.stat()
            if (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size):
                raise ValueError('Исходник изменился во время анализа.')
            rows.append(dict(channel=int(key),filename=path.name,size=after.st_size,sha256=sha))
        return sorted(rows,key=lambda r:r['channel'])
