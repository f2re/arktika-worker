"""Интеграция полевых слоёв с одним вычислительным слотом рабочего места."""
from __future__ import annotations
import copy
import datetime as dt
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
from ..download import atomic_json, digest
from ..era5_runtime import ensure_runtime, run_process, runtime_env
from ..network import Cancelled
from .catalog import FIELDS, SOURCES, UTC, identity, iso, request, plan
from .storage import load, listing, safe_id
from .render import style, sample

ROOT = Path(__file__).resolve().parents[2]


class ReanalysisMixin:
    def _fields_init(self):
        with self.lock:
            if hasattr(self, '_fields_root'): return
            self._fields_root = self.store.root / 'reanalysis'
            for name in ('fields', 'renders'):
                (self._fields_root / name).mkdir(parents=True, exist_ok=True)
            self._field_layers = self.store.setting('reanalysis_layers', [])
            self._field_job = {'status': 'idle'}
            p = self._fields_root / 'job.json'
            if p.is_file():
                try:
                    self._field_job = json.loads(p.read_text(encoding='utf-8'))
                    if self._field_job.get('status') == 'running':
                        self._field_job.update(status='interrupted', message='Подготовка прервана перезапуском. Завершённые поля доступны в кэше.')
                except (OSError, ValueError): pass

    def reanalysis_state(self):
        self._fields_init()
        with self.lock:
            layers = copy.deepcopy(self._field_layers); job = copy.deepcopy(self._field_job)
        rows = listing(self._fields_root / 'fields')
        size = sum(p.stat().st_size for d in ('fields', 'renders') for p in (self._fields_root / d).glob('*/*') if p.is_file())
        return {'layers': layers, 'fields': rows[:100], 'job': job, 'busy': self.busy, 'cache_bytes': size}

    def _field_status(self, **kwargs):
        with self.lock:
            self._field_job.update(kwargs)
            atomic_json(self._fields_root / 'job.json', self._field_job)
        if kwargs.get('message'): self.log(kwargs['message'])

    def _field_run(self, payload, runtime, temp):
        result_file = Path(temp) / (uuid.uuid4().hex + '.json')
        env = runtime_env(self.store.root)
        env['PYTHONPATH'] = str(ROOT) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        code = run_process([runtime['python'], '-m', 'arktika.reanalysis.worker'], self.cancel,
                           timeout=3600, env=env, payload=dict(payload, root=str(self._fields_root), result_file=str(result_file)))
        if not result_file.is_file():
            raise ValueError('Обработчик поля не завершился. Проверьте библиотеки и повторите; готовые поля не удалены.')
        result = json.loads(result_file.read_text(encoding='utf-8'))
        if code or not result.get('ok'):
            raise ValueError(result.get('error', 'Обработка поля не завершена.'))
        return result

    def _field_start(self, title, fn):
        self._fields_init()
        with self.lock:
            if self.busy: raise ValueError('Другая операция выполняется. Завершите или отмените её.')
            job_id = uuid.uuid4().hex
            self._field_status(id=job_id, status='running', message=title)
            def work():
                try:
                    runtime = ensure_runtime(self.store.root, self.cancel, lambda m: self._field_status(message=m))
                    with tempfile.TemporaryDirectory(prefix='job-', dir=self._fields_root) as temp:
                        result = fn(runtime, Path(temp))
                    self._field_status(status='done', message='Полевые слои готовы.')
                    return {'reanalysis_job': job_id, **result}
                except Cancelled:
                    self._field_status(status='cancelled', message='Подготовка слоя отменена. Готовый кэш сохранён.')
                    raise
                except Exception as exc:
                    message = str(exc) if isinstance(exc, ValueError) else 'Подготовка слоя не завершена. Готовый кэш сохранён.'
                    self._field_status(status='error', message=message)
                    raise ValueError(message) from None
            self.start(title, work)
        return {'started': True, 'job_id': job_id}

    def _field_map(self, identity_value, options, preset, width, runtime, temp):
        self._field_status(message='Наношу поле на карту; исходное разрешение сохраняется.')
        r = self._field_run({'action': 'render', 'id': identity_value, 'preset': preset, 'width': width,
                             'style': options, 'output': str(temp)}, runtime, temp)['render']
        return {k: r[k] for k in ('id', 'preset', 'width', 'height', 'crs', 'bounds', 'valid_pixels')}

    def _field_add(self, meta, options, view):
        with self.lock:
            if len(self._field_layers) >= 8:
                raise ValueError('На карте уже 8 полевых слоёв. Удалите ненужный слой и откройте поле из кэша.')
            layer = {'id': uuid.uuid4().hex[:24], 'field_id': meta['id'], 'title': meta['title'], 'name': meta['name'],
                     'source': meta['source'], 'field': meta['field'], 'time': meta['time'], 'level': meta['level'],
                     'temporal': meta['temporal'], 'units': meta['units'], 'vector': meta['vector'],
                     'resolution_km': meta['resolution_km'], 'visible': True, 'style': options,
                     'views': {view['preset'] + ':' + str(view['width']): view}}
            self._field_layers.append(layer)
            self.store.set_setting('reanalysis_layers', self._field_layers)
        return layer

    def reanalysis_prepare(self, data):
        self._fields_init(); self._sources_init()
        r = request(data)
        action = 'import' if data.get('path') else 'fetch'
        source_file = None
        if action == 'import':
            source_file = Path(str(data['path'])).expanduser()
            if not source_file.is_absolute() or not source_file.is_file() or source_file.suffix.lower() not in ('.nc', '.nc4', '.grib', '.grb', '.grib2', '.zip'):
                raise ValueError('Укажите абсолютный путь на компьютере сервера к NetCDF/GRIB источника.')
            if source_file.stat().st_size > 512 * 1024 * 1024:
                raise ValueError('Локальный файл больше 512 МиБ. Подготовьте поднабор.')
        key = dict(r)
        if source_file: key['source_sha256'] = digest(source_file)
        if data.get('refresh') is True: key['refresh'] = uuid.uuid4().hex
        field_id = identity(key)
        target = self._fields_root / 'fields' / field_id
        credential = copy.deepcopy(self._earthdata_credential if r['source'] == 'merra2' else self._era5_credential)
        if action == 'fetch' and not target.is_dir() and not credential:
            raise ValueError('Настройте ' + ('Earthdata' if r['source'] == 'merra2' else 'Copernicus CDS') + ' в «Источниках данных».')
        meta_preview = dict(field=r['field'], vector=FIELDS[r['field']]['kind'] == 'wind')
        options = style(data.get('style', {}), meta_preview)
        preset, width = self._field_grid(data)
        def work(runtime, temp):
            if target.is_dir():
                _, meta = load(self._fields_root / 'fields', field_id)
                self._field_status(message='Использую проверенный локальный кэш; сетевой запрос не нужен.')
            else:
                if shutil.disk_usage(self._fields_root).free < 1024 ** 3:
                    raise ValueError('Для подготовки поля требуется не менее 1 ГиБ свободного диска.')
                output = temp / 'field'; output.mkdir()
                self._field_status(message='Читаю локальный файл.' if source_file else 'Запрашиваю ' + SOURCES[r['source']]['name'] + ': ' + FIELDS[r['field']]['name'] + '.')
                payload = {'action': action, 'request': r, 'credential': credential, 'id': field_id, 'output': str(output)}
                if source_file: payload['path'] = str(source_file)
                meta = self._field_run(payload, runtime, temp)['field']
                if self.cancel.is_set(): raise Cancelled()
                output.rename(target)
            view = self._field_map(field_id, options, preset, width, runtime, temp)
            self._field_add(meta, options, view)
            return {'field_id': field_id}
        return self._field_start('Подготовка поля реанализа', work)

    def _field_grid(self, data):
        from ..geo import grid
        preset = data.get('preset', 'arctic'); width = int(data.get('width', 1000))
        grid(preset, width)
        return preset, width

    def reanalysis_layers(self, data):
        self._fields_init()
        action = data.get('action')
        if action == 'cached':
            field_id = safe_id(data.get('field_id')); _, meta = load(self._fields_root / 'fields', field_id)
            options = style(data.get('style', {}), meta); preset, width = self._field_grid(data)
            return self._field_start('Открываю поле из кэша', lambda runtime, temp: {'layer': self._field_add(meta, options, self._field_map(field_id, options, preset, width, runtime, temp))})
        with self.lock:
            layer = next((l for l in self._field_layers if l['id'] == data.get('id')), None)
            if not layer: raise ValueError('Слой не найден.')
            if action == 'remove':
                self._field_layers.remove(layer)
            elif action == 'move':
                pos = self._field_layers.index(layer); step = int(data.get('step', 0))
                if step not in (-1, 1): raise ValueError('Перемещение: на один слой вверх или вниз.')
                self._field_layers.pop(pos); self._field_layers.insert(max(0, min(len(self._field_layers), pos + step)), layer)
            elif action == 'update':
                if 'visible' in data:
                    if not isinstance(data['visible'], bool): raise ValueError('Видимость: true/false.')
                    layer['visible'] = data['visible']
                if 'style' in data:
                    new = style(dict(layer['style'], **data['style']), layer)
                    if any(new[k] != layer['style'][k] for k in ('min', 'max', 'step')): layer['views'] = {}
                    layer['style'] = new
            else: raise ValueError('Неизвестное действие над слоями.')
            self.store.set_setting('reanalysis_layers', self._field_layers)
        return self.reanalysis_state()

    def reanalysis_map(self, data):
        self._fields_init(); preset, width = self._field_grid(data)
        with self.lock:
            selected = [copy.deepcopy(l) for l in self._field_layers if l['visible'] and preset + ':' + str(width) not in l['views']]
        if not selected: return {'started': False}
        def work(runtime, temp):
            for layer in selected:
                view = self._field_map(layer['field_id'], layer['style'], preset, width, runtime, temp)
                with self.lock:
                    current = next((l for l in self._field_layers if l['id'] == layer['id']), None)
                    if current and all(current['style'][k] == layer['style'][k] for k in ('min', 'max', 'step')):
                        current['views'][preset + ':' + str(width)] = view
                        self.store.set_setting('reanalysis_layers', self._field_layers)
            return {'map_ready': True}
        return self._field_start('Перепроецирование полевых слоёв', work)

    def reanalysis_difference(self, data):
        self._fields_init()
        left, right = safe_id(data.get('left')), safe_id(data.get('right'))
        if left == right: raise ValueError('Выберите два разных поля.')
        field_id = identity({'operation': 'difference-v1', 'left': left, 'right': right})
        preset, width = self._field_grid(data); target = self._fields_root / 'fields' / field_id
        def work(runtime, temp):
            if target.is_dir(): _, meta = load(self._fields_root / 'fields', field_id)
            else:
                output = temp / 'difference'; output.mkdir()
                meta = self._field_run({'action': 'difference', 'left': left, 'right': right, 'id': field_id, 'output': str(output)}, runtime, temp)['field']
                if self.cancel.is_set(): raise Cancelled()
                output.rename(target)
            options = style(data.get('style', {}), meta)
            self._field_add(meta, options, self._field_map(field_id, options, preset, width, runtime, temp))
            return {'field_id': field_id}
        return self._field_start('Разность на более грубой исходной сетке', work)

    def reanalysis_point(self, data):
        self._fields_init()
        lon, lat = float(data.get('lon')), float(data.get('lat'))
        if not math.isfinite(lon + lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise ValueError('Неверные координаты точки.')
        with self.lock: layers = [copy.deepcopy(l) for l in self._field_layers if l['visible']]
        rows = []
        for layer in layers:
            arrays, meta = load(self._fields_root / 'fields', layer['field_id'])
            p = sample(arrays, meta, np_array(lon), np_array(lat), tree=False)
            row = {k: layer[k] for k in ('field_id', 'title', 'name', 'time', 'level', 'temporal', 'units')}
            row.update({k: float(v[0]) if math.isfinite(float(v[0])) else None for k, v in p.items()})
            row['native_lat']=row.pop('lat',None);row['native_lon']=row.pop('lon',None)
            rows.append(row)
        return {'lon': lon, 'lat': lat, 'fields': rows, 'method': 'nearest_native_node', 'note': 'Значения исходных узлов, не цвета PNG; пропуск не равен нулю.'}

    def reanalysis_cancel(self, data):
        self._fields_init()
        with self.lock:
            if self._field_job.get('status') != 'running' or data.get('id') != self._field_job.get('id'):
                raise ValueError('Эта подготовка уже не выполняется; другая операция не отменена.')
            self.cancel.set()
        return {'cancelled': True}

    def reanalysis_artifact(self, kind, identity_value, name):
        self._fields_init(); safe_id(identity_value)
        if kind not in ('render', 'field'): raise ValueError('Неверный тип полевого продукта.')
        allowed = ('map.png', 'values.tif', 'render.json') if kind == 'render' else ('field.nc', 'field.json')
        if name not in allowed: raise ValueError('Этот файл не относится к полевому продукту.')
        p = self._fields_root / ('renders' if kind == 'render' else 'fields') / identity_value / name
        ready = p.parent / ('render.json' if kind == 'render' else 'field.json')
        if not ready.is_file() or not p.is_file(): raise ValueError('Полевой продукт ещё не готов.')
        if kind == 'field' and name == 'field.nc':
            meta = json.loads(ready.read_text(encoding='utf-8'))
            if digest(p) != meta.get('netcdf_sha256'): raise ValueError('Контрольная сумма NetCDF не совпала.')
        return p


def np_array(value):
    import numpy as np
    return np.array([value], float)
