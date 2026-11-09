"""Persistence and API boundary for the scientific point inspector."""
from __future__ import annotations
import hashlib
import json
import re
import math
from pathlib import Path
from .download import atomic_json
from .interpretation import parse_profile, spectral, profile_diagnostics, applicability, utc, VERSION
from .source_validation import reject_explicit_synthetic_metadata


def _require_real_profile_input(data):
    """Check the original declaration before profile normalization drops keys."""
    reject_explicit_synthetic_metadata(data)
    if isinstance(data, dict):
        reject_explicit_synthetic_metadata(data.get('metadata'))
        text = data.get('text')
        if isinstance(text, str) and text.lstrip('\ufeff \t\r\n').startswith('{'):
            reject_explicit_synthetic_metadata(json.loads(text.lstrip('\ufeff')))


class AnalysisMixin:
    def _analysis_root(self, name):
        root = self.store.root/name
        root.mkdir(exist_ok=True)
        return root

    def import_profile(self, data):
        _require_real_profile_input(data)
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
            try:
                _require_real_profile_input(data)
            except ValueError:
                continue
            result.append({k:data[k] for k in ('id','source','valid_time','lat','lon','radius_km','max_hours')})
        return sorted(result,key=lambda r:r['valid_time'],reverse=True)

    def get_profile(self, identity):
        if not re.fullmatch('[0-9a-f]{24}', str(identity)):
            raise ValueError('Выберите профиль.')
        path = self._analysis_root('profiles')/(identity+'.json')
        if not path.is_file():
            raise ValueError('Профиль не найден.')
        data = json.loads(path.read_text(encoding='utf-8'))
        _require_real_profile_input(data)
        return data

    def _extract_era5_profile(self, nc_path, lat, lon, time_str):
        try:
            import xarray as xr
            import numpy as np
            with xr.open_dataset(nc_path) as ds:
                reject_explicit_synthetic_metadata(ds.attrs)
                for variable in ds.data_vars.values():
                    reject_explicit_synthetic_metadata(variable.attrs)
                time_name = next((name for name in ('valid_time', 'time') if name in ds.coords), None)
                if time_name is None:
                    return None
                time_coord = ds[time_name]
                if time_coord.ndim > 1 or not np.issubdtype(time_coord.dtype, np.datetime64):
                    return None
                wanted = np.datetime64(utc(time_str).replace(tzinfo=None), 'ns')
                times = np.asarray(time_coord.values).reshape(-1).astype('datetime64[ns]')
                hits = np.flatnonzero(times == wanted)
                if hits.size != 1:
                    return None
                actual_time = times[int(hits[0])]
                if time_coord.dims:
                    ds = ds.isel({time_coord.dims[0]: int(hits[0])})
                ds_lons = ds.longitude.values
                target_lon = (lon % 360) if ds_lons.min() >= 0 else lon
                pt = ds.sel(latitude=lat, longitude=target_lon, method='nearest').squeeze()
                # ECMWF's spherical geometric-height approximation. Phi/g0 is
                # geopotential height and must not be labelled geometric AMSL.
                geopotential_height = np.asarray(pt.z, float) / 9.80665
                earth_radius = 6371229.0
                z = np.divide(earth_radius * geopotential_height,
                              earth_radius - geopotential_height,
                              out=np.full_like(geopotential_height, np.nan),
                              where=np.isfinite(geopotential_height) & (np.abs(geopotential_height) < earth_radius))
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
                time_iso = np.datetime_as_string(actual_time, unit='us') + 'Z'
                actual_lon = float(pt.longitude if pt.longitude <= 180 else pt.longitude - 360)
                prof_data = {
                    'source': f"ERA5 Reanalysis (ECMWF, {time_iso[:16].replace('T', ' ')} UTC); геометрическая высота — сферическое приближение",
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
                    'note': 'Профиль ERA5 из локального кэша; фактический срок CF совпадает с метаданными кэша. '
                            'Геометрическая высота приближена по ECMWF: h=Φ/9.80665, z=R·h/(R−h), R=6371229 м; '
                            'горизонтальные изменения ускорения тяжести не учтены.'
                }
                if 'q' in pt.data_vars:
                    q = np.asarray(pt.q, float)[valid][idx][good]
                    if np.all(np.isfinite(q)) and np.all(q >= 0):
                        prof_data['specific_humidity'] = q.tolist()
                        prof_data['humidity_units'] = 'kg/kg'
                result = parse_profile(prof_data)
                result['data_kind'] = 'era5'
                result['note'] = prof_data['note']
                return result
        except Exception:
            return None

    def resolve_auto_profile(self, lat, lon, time_str):
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
                        prof = self._extract_era5_profile(nc_path, lat, lon, item.get('time'))
                        if prof and applicability(prof, lon, lat, time_str)['applicable']:
                            best_prof = prof
                            best_dist = dist
                            if dist < 300:
                                break
                except Exception:
                    continue
            if best_prof and best_dist < 1500:
                best_prof['id'] = hashlib.sha256(json.dumps(best_prof, sort_keys=True).encode()).hexdigest()[:24]
                return best_prof

        return None

    def analyse(self, data):
        if data.get('demo_profile') is True:
            raise ValueError('Демонстрационный профиль отключён. Загрузите реальный профиль наблюдений или модели.')
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
                profile = self.resolve_auto_profile(pixel['lat'], pixel['lon'], pixel['time'])
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
        content = path.read_bytes()
        saved = json.loads(content)
        reject_explicit_synthetic_metadata(saved)
        _require_real_profile_input(saved.get('profile_snapshot'))
        return content

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
