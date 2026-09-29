"""Изолированные сетевые и численные операции. Секреты только на stdin; stdout не используется."""
from __future__ import annotations
import datetime as dt
import json
import sys
from pathlib import Path
from .catalog import request, identity, iso, UTC
from .adapters import read_local, retrieve, file_digest
from .storage import save, load
from .render import render, difference
from ..download import atomic_json


def execute(payload):
    root = Path(payload['root']); output = Path(payload['output'])
    action = payload['action']
    if action in ('fetch', 'import'):
        r = request(payload['request'])
        if action == 'import':
            before = file_digest(payload['path'])
            arrays, meta = read_local(payload['path'], r)
            if file_digest(payload['path']) != before:
                raise ValueError('Исходный файл изменился во время чтения.')
            meta['provenance'] = {'transport': 'local_file', 'filename': Path(payload['path']).name, 'source_sha256': before}
        else:
            arrays, meta = retrieve(r, payload.get('credential', {}), output)
        meta.update(id=payload['id'], created_at=iso(dt.datetime.now(UTC)))
        meta = save(output, arrays, meta)
        return {'field': meta}
    if action == 'difference':
        a, am = load(root / 'fields', payload['left']); b, bm = load(root / 'fields', payload['right'])
        arrays, meta = difference(a, am, b, bm)
        meta.update(id=payload['id'], created_at=iso(dt.datetime.now(UTC)))
        return {'field': save(output, arrays, meta)}
    if action == 'render':
        arrays, meta = load(root / 'fields', payload['id'])
        return {'render': render(arrays, meta, payload['preset'], int(payload['width']), payload.get('style', {}), root / 'renders')}
    raise ValueError('Неизвестная операция полевого обработчика.')


def main():
    payload = json.loads(sys.stdin.buffer.read(131072))
    result_file = Path(payload['result_file'])
    try:
        result = {'ok': True, **execute(payload)}
    except ValueError as exc:
        # Only our own validation errors are safe to show. Library errors may contain signed URLs.
        frames = []
        tb = exc.__traceback__
        while tb:
            frames.append(tb.tb_frame.f_globals.get('__name__', '')); tb = tb.tb_next
        own = frames[-1].startswith('arktika.reanalysis') if frames else False
        result = {'ok': False, 'error': str(exc) if own else 'Не удалось прочитать или получить поле. Проверьте формат, доступ и условия набора данных.'}
    except Exception as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if isinstance(exc, ImportError):
            message = 'Не установлены библиотеки реанализа. Запустите установку зависимостей приложения.'
        elif status in (401, 403):
            message = 'Источник отказал в доступе. Проверьте токен и принятие условий; для Earthdata также разрешения GES DISC / Hyrax.'
        elif status == 404:
            message = 'Источник не публикует этот файл/срок. Дата не заменена другим днём.'
        else:
            message = 'Загрузка/обработка не завершена. Проверьте соединение, доступ, срок и набор данных; готовый кэш сохранён.'
        result = {'ok': False, 'error': message}
    atomic_json(result_file, result)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
