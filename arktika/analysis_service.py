"""Persistence and API boundary for the scientific point inspector."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from .download import atomic_json
from .interpretation import parse_profile, spectral, profile_diagnostics, VERSION


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
        if data.get('profile_id'):
            profile = self.get_profile(data['profile_id'])
            result['profile_result'] = profile_diagnostics(profile,pixel,analysis,
                    data.get('altitude_m'),data.get('cloud_confirmed') is True,
                    data.get('opaque_confirmed') is True,data.get('delta_k',2))
            result['profile_id'] = profile['id']
            result['profile_snapshot'] = profile
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
