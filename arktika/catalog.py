"""Комплектность каталога и локальных файлов, отдельно от физической калибровки."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BUNDLES = [
    dict(id='channel', title='Отдельный канал', channels=None),
    dict(id='micro24', title='Микрофизика · 24 ч', channels=[7, 9, 10]),
    dict(id='night', title='Низкие облака · ночь', channels=[4, 9, 10]),
    dict(id='phase', title='Признаки фазы · эксперимент', channels=[7, 9, 10]),
    dict(id='difference', title='Разность оконных каналов', channels=[9, 10]),
    dict(id='dust', title='Спектральный контраст пыли', channels=[7, 9, 10]),
    dict(id='ash', title='Спектральный контраст пепла', channels=[7, 9, 10]),
    dict(id='indicators', title='Совместные спектральные признаки', channels=[4, 9, 10]),
    dict(id='ir', title='Все ИК-каналы · 4–10', channels=list(range(4, 11))),
    dict(id='all', title='Все каналы · 1–10', channels=list(range(1, 11))),
]
ACTIVE_STATES = {'queued', 'running'}


def channel_inventory(assets: list[dict], local: dict | None, jobs: dict) -> list[dict]:
    """Один предпочтительный файл на канал, не один файл на каждую его проекцию.

    Локальные каналы передаёт Workstation.scenes после проверки существования.
    Завершённая запись очереди сама по себе не означает пригодного локального TIFF.
    """
    local_channels = {c['channel'] for c in (local or {}).get('channels', [])}
    result = []
    for ch in range(1, 11):
        candidates = [a for a in assets if a.get('category') == 'channel' and a.get('channel') == ch]
        candidates.sort(key=lambda a: (a.get('epsg') != 4326, a.get('size') is None,
                                       a.get('size') or 0, a['id']))
        item = dict(channel=ch, state='absent', candidate_id=None, size=None,
                    variants=len(candidates), present=bool(candidates) or ch in local_channels)
        if ch in local_channels:
            item['state'] = 'local'
        else:
            active = [a for a in candidates if jobs.get(a['id'], {}).get('state') in ACTIVE_STATES]
            # Не скачиваем второй вариант, пока первый уже в очереди или загружается.
            if active:
                item['state'] = 'running' if any(jobs[a['id']]['state'] == 'running' for a in active) else 'queued'
            elif candidates:
                usable = []
                for a in candidates:
                    job = jobs.get(a['id'], {})
                    if job.get('state') == 'done' and Path(job.get('path', '')).is_file():
                        continue  # файл есть, но Workstation не смог зарегистрировать канал
                    usable.append(a)
                if usable:
                    preferred = usable[0]
                    state = jobs.get(preferred['id'], {}).get('state', '')
                    item.update(state=state if state in ('paused', 'error') else 'remote',
                                candidate_id=preferred['id'], size=preferred.get('size'))
                else:
                    item['state'] = 'unregistered'
        result.append(item)
    return result


def describe_session(assets: list[dict], local: dict | None, jobs: dict) -> dict:
    inventory = channel_inventory(assets, local, jobs)
    return dict(local_id=(local or {}).get('id'), channel_inventory=inventory,
                local_channels=[c['channel'] for c in inventory if c['state'] == 'local'],
                catalog_channels=sorted({a['channel'] for a in assets if a.get('category') == 'channel'
                                         and 1 <= a.get('channel', 0) <= 10}),
                queue=dict(Counter(jobs[a['id']]['state'] for a in assets if a['id'] in jobs)),
                search_text=' '.join(str(a.get(k) or '') for a in assets+(local or {}).get('channels', [])
                                     for k in ('filename', 'title', 'level')))


def _asset_matches(asset: dict, filters: dict) -> bool:
    category, channel, epsg = (filters.get('category', 'all'), filters.get('channel', 0), filters.get('epsg', 0))
    query = str(filters.get('query', '')).casefold().strip()
    haystack = ' '.join(str(asset.get(k) or '') for k in ('name', 'title', 'filename', 'item_id', 'level', 'time', 'platform')).casefold()
    return ((category == 'all' or asset.get('category') == category)
            and (not channel or asset.get('channel') == channel)
            and (not epsg or asset.get('epsg') == epsg)
            and (not query or all(word in haystack for word in query.split())))


def _session_matches(summary: dict, assets: list[dict], local: dict | None, filters: dict) -> bool:
    """Категория/канал/проекция должны совпасть у одного файла, не у разных."""
    category, channel, epsg = (filters.get('category', 'all'), filters.get('channel', 0), filters.get('epsg', 0))
    if category != 'all' or channel or epsg:
        candidates = assets + [dict(c, category='channel', epsg=_epsg(c.get('crs', '')))
                               for c in (local or {}).get('channels', [])]
        if not any((category == 'all' or c.get('category') == category)
                   and (not channel or c.get('channel') == channel)
                   and (not epsg or c.get('epsg') == epsg) for c in candidates):
            return False
    query = str(filters.get('query', '')).casefold().strip()
    haystack = ' '.join([summary['platform'], summary['time'], summary['search_text'], *summary['levels']]).casefold()
    return not query or all(word in haystack for word in query.split())


def _epsg(crs: str) -> int:
    try:
        return int(crs.split(':', 1)[1]) if crs.startswith('EPSG:') else 0
    except (ValueError, IndexError):
        return 0


def catalog_sessions(app: Any, day: str, filters: dict, offset: int = 0, limit: int = 24) -> dict:
    """Объединённый список сроков. Читает только локальный каталог, не сеть."""
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc)
    platform = filters.get('platform', '')
    if platform not in ('', 'ARCM1', 'ARCM2'):
        raise ValueError('Неизвестный аппарат.')
    assets = app.store.assets(day, {'platform': platform})
    scenes = app.scenes(day, platform)
    records = app.store.records(day, platform)
    grouped: dict[tuple[str, str], dict] = {}
    for rows, kind in ((assets, 'assets'), (scenes, 'scenes'), (records, 'records')):
        for row in rows:
            if row.get('platform') not in ('ARCM1', 'ARCM2') or not str(row.get('time', '')).startswith(day+'T'):
                continue
            key = row['platform'], row['time']
            grouped.setdefault(key, {'assets': [], 'scenes': [], 'records': []})[kind].append(row)
    jobs = {j['id']: j for j in app.store.jobs()}
    sessions, visible_assets = [], []
    for (satellite, stamp), group in sorted(grouped.items(), key=lambda x: (x[0][1], x[0][0]), reverse=True):
        aa, recs = group['assets'], group['records']
        local = group['scenes'][0] if group['scenes'] else None
        summary = dict(platform=satellite, time=stamp, count=len(aa),
                       size=sum(a.get('size') or 0 for a in aa),
                       unknown_sizes=sum(a.get('size') is None for a in aa),
                       categories=dict(Counter(a['category'] for a in aa)),
                       levels=sorted({str(a.get('level') or '') for a in aa+recs} - {''}),
                       ids=[a['id'] for a in aa], no_uri=not bool(aa), record_count=len(recs))
        summary.update(describe_session(aa, local, jobs))
        summary['search_text'] += ' ' + ' '.join(str(r.get(k) or '') for r in recs for k in ('id', 'title', 'level'))
        previews = [a for a in aa if a.get('category') == 'image']
        summary.update(preview=previews[0]['id'] if previews else '',
                       preview_caption=('Канал '+str(previews[0]['channel']) if previews[0].get('channel') else 'Обзор') if previews else '')
        if _session_matches(summary, aa, local, filters):
            selected = [a for a in aa if _asset_matches(a, filters)]
            summary.update(ids=[a['id'] for a in selected], count=len(selected),
                           size=sum(a.get('size') or 0 for a in selected),
                           unknown_sizes=sum(a.get('size') is None for a in selected),
                           categories=dict(Counter(a['category'] for a in selected)))
            sessions.append(summary)
            visible_assets.extend(selected)
    stamp = lambda d: d.isoformat(timespec='seconds').replace('+00:00', 'Z')
    coverage = app.store.coverage(stamp(start), stamp(start+timedelta(days=1)),
                                  [platform] if platform else ('ARCM1', 'ARCM2'))
    clock = datetime.now(timezone.utc)
    if start > clock:
        coverage = 'future'
    elif start.date() == clock.date() and coverage == 'unknown':
        coverage = 'current_unchecked'
    offset, limit = max(0, int(offset)), min(60, max(1, int(limit)))
    return dict(sessions=sessions[offset:offset+limit], total=len(sessions), offset=offset,
                files=len(visible_assets), ids=[a['id'] for a in visible_assets],
                size=sum(a.get('size') or 0 for a in visible_assets),
                unknown_sizes=sum(a.get('size') is None for a in visible_assets),
                coverage=coverage, bundles=BUNDLES)
