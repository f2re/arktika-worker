"""Локальный индекс и очередь. Неполный импорт не становится полным месяцем."""
import csv
import datetime as dt
import json
import os
import sqlite3
import threading
from pathlib import Path
from .model import normalize_item, normalize_asset, PLATFORMS, matches, iso, now


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        
        if os.name != 'nt':
            os.chmod(str(self.root), 0o700)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.root / 'catalog.sqlite'), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, platform TEXT, stamp TEXT, data TEXT);
        CREATE INDEX IF NOT EXISTS item_stamp ON items(stamp,platform);
        CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, item_id TEXT, platform TEXT, stamp TEXT, data TEXT);
        CREATE INDEX IF NOT EXISTS asset_stamp ON assets(stamp,platform);
        CREATE TABLE IF NOT EXISTS scopes(platform TEXT, start TEXT, stop TEXT, status TEXT, checked TEXT,
            count INTEGER, message TEXT, PRIMARY KEY(platform,start,stop));
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, asset_id TEXT, state TEXT, done INTEGER,
            total INTEGER, path TEXT, error TEXT, sha256 TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        ''')
        self.conn.execute("UPDATE jobs SET state='paused' WHERE state IN ('running','queued')")
        self.conn.commit()
        
        if os.name != 'nt':
            os.chmod(str(self.root / 'catalog.sqlite'), 0o600)
        self.revision = 0

    def close(self):
        with self.lock:
            self.conn.close()

    def setting(self, key, default=None):
        with self.lock:
            row = self.conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.lock, self.conn:
            self.conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))

    def upsert(self, records, assets):
        with self.lock, self.conn:
            for rec in records:
                if not rec or not rec.get('id'):
                    continue
                old = self.conn.execute('SELECT data FROM items WHERE id=?', (rec['id'],)).fetchone()
                if old:
                    merged = json.loads(old[0]); merged.update({k: v for k, v in rec.items() if v not in ('', None, [])})
                    rec = merged
                self.conn.execute('INSERT OR REPLACE INTO items VALUES(?,?,?,?)',
                                  (rec['id'], rec['platform'], rec['time'], json.dumps(rec, ensure_ascii=False)))
            for a in assets:
                if not a:
                    continue
                old = self.conn.execute('SELECT data FROM assets WHERE id=?', (a['id'],)).fetchone()
                if old:
                    b = json.loads(old[0])
                    # TSV без bands/размера не стирает более полные метаданные JSON.
                    b.update({k: v for k, v in a.items() if v not in ('', None, [], 0)})
                    a = b
                self.conn.execute('INSERT OR REPLACE INTO assets VALUES(?,?,?,?,?)',
                                  (a['id'], a['item_id'], a['platform'], a['time'], json.dumps(a, ensure_ascii=False)))
            self.revision += 1

    def assets(self, prefix='', filters=None, item_id=None):
        q, params = 'SELECT data FROM assets WHERE stamp LIKE ?', [prefix + '%']
        if item_id:
            q += ' AND item_id=?'; params.append(item_id)
        q += ' ORDER BY stamp DESC, platform, id'
        with self.lock:
            rows = [json.loads(r[0]) for r in self.conn.execute(q, params)]
        return [a for a in rows if matches(a, filters or {})]

    def asset(self, identity):
        with self.lock:
            r = self.conn.execute('SELECT data FROM assets WHERE id=?', (identity,)).fetchone()
        return json.loads(r[0]) if r else None

    def records(self, prefix='', platform=''):
        with self.lock:
            rows = [json.loads(r[0]) for r in self.conn.execute(
                'SELECT data FROM items WHERE stamp LIKE ? ORDER BY stamp DESC,platform', (prefix + '%',))]
        return [r for r in rows if not platform or r['platform'] == platform]

    def scope(self, platform, start, stop, status, count=0, message=''):
        with self.lock, self.conn:
            self.conn.execute('INSERT OR REPLACE INTO scopes VALUES(?,?,?,?,?,?,?)',
                              (platform, start, stop, status, iso(now()), count, message))
            self.revision += 1

    def coverage(self, start, stop, platforms=PLATFORMS):
        """Полнота только если имеется успешный запрос, покрывающий весь интервал."""
        states = []
        with self.lock:
            for p in platforms:
                rows = self.conn.execute('SELECT * FROM scopes WHERE platform=? AND start<=? AND stop>=? ORDER BY checked DESC',
                                         (p, start, stop)).fetchall()
                states.append(rows[0]['status'] if rows else 'unknown')
        if all(s == 'complete' for s in states):
            return 'complete'
        if 'error' in states:
            return 'error'
        if 'partial' in states:
            return 'partial'
        return 'unknown'

    def jobs(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute('SELECT * FROM jobs ORDER BY created,id')]

    def job(self, identity):
        with self.lock:
            r = self.conn.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone()
        return dict(r) if r else None

    def enqueue(self, asset, path):
        with self.lock, self.conn:
            old = self.conn.execute('SELECT state FROM jobs WHERE id=?', (asset['id'],)).fetchone()
            if old:
                if old[0] not in ('done', 'running'):
                    self.conn.execute("UPDATE jobs SET state='queued',error='' WHERE id=?", (asset['id'],))
            else:
                self.conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                                  (asset['id'], asset['id'], 'queued', 0, asset['size'], str(path), '', '', iso(now())))
            self.revision += 1

    def update_job(self, identity, **values):
        allowed = {'state', 'done', 'total', 'path', 'error', 'sha256'}
        if not values or set(values) - allowed:
            raise ValueError('Недопустимые поля очереди.')
        with self.lock, self.conn:
            self.conn.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                              list(values.values()) + [identity])
            self.revision += 1

    def diagnostic(self):
        with self.lock:
            scopes = [dict(r) for r in self.conn.execute('SELECT * FROM scopes ORDER BY checked DESC LIMIT 100')]
            n = self.conn.execute('SELECT count(*) FROM items').fetchone()[0]
            a = self.conn.execute('SELECT count(*) FROM assets').fetchone()[0]
        from collections import Counter
        return {'application': 'Arktika Web 2.0.0', 'time': iso(now()), 'items': n, 'assets': a,
                'categories': dict(Counter(x['category'] for x in self.assets())),
                'queue': dict(Counter(j['state'] for j in self.jobs())),
                'scopes': [{k: r[k] for k in ('platform', 'start', 'stop', 'status', 'checked', 'count')} for r in scopes]}


def import_path(store, path, callback=None):
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise ValueError('Путь импорта не существует.')
    database=path if path.is_file() and path.suffix in ('.sqlite','.sqlite3','.db') else path/'catalog.sqlite'
    if database.is_file():
        return import_sqlite(store,database,callback)
    if path.is_file():
        paths = [path]
    else:
        patterns = ['*assets*LOCAL*.tsv', '*assets*LOCAL*.json', '*channel_products*LOCAL*.json',
                    '*raw_candidates*.json', '*science_candidates*.json', '*arktika_items*.json',
                    '*stac*.json', 'responses/*stac*.json', 'fixture_catalog.json']
        paths = sorted(set(p for pattern in patterns for p in path.glob(pattern) if p.is_file()),
                       key=lambda p: (p.stat().st_mtime, p.name))
    files, recs, count = 0, 0, 0
    for p in paths:
        if p.stat().st_size > 256 * 1024 * 1024:
            continue
        if callback:
            callback('Импорт: ' + p.name)
        try:
            if p.suffix.lower() == '.tsv':
                with p.open(encoding='utf-8-sig', newline='') as f:
                    data = list(csv.DictReader(f, delimiter='\t'))
            else:
                with p.open(encoding='utf-8-sig') as f:
                    data = json.load(f)
        except (ValueError, UnicodeError, OSError):
            continue
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) and (data.get('type') == 'Feature' or 'assets' in data) else data.get('features', data.get('results', [])) if isinstance(data, dict) else [])
        if not isinstance(rows, list):
            continue
        records, assets = [], []
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw = row.get('record', row)
            if not isinstance(raw, dict):
                continue
            if raw.get('type') == 'Feature' or 'assets' in raw or 'platform_identifier' in raw:
                r, found = normalize_item(raw, 'import')
                if r:
                    records.append(r); assets.extend(found)
            elif raw.get('uri') or raw.get('href'):
                if platform_of_flat(raw) not in PLATFORMS:
                    continue
                a = normalize_asset(raw)
                if a:
                    assets.append(a)
                    records.append({'id': a['item_id'], 'platform': a['platform'], 'time': a['time'],
                                    'time_original': a['time_original'], 'time_assumed': a['time_assumed'],
                                    'level': a['level'], 'source': 'import', 'title': a['item_id']})
        if records or assets:
            store.upsert(records, assets); files += 1; recs += len(records); count += len(assets)
    # share_report счётчики не являются доказательством, что все страницы приложены.
    return {'files': files, 'rows': recs, 'assets_processed': count,
            'unique_assets': len(store.assets()), 'unique_items': len(store.records())}


def platform_of_flat(row):
    return row.get('platform') or ''


def import_sqlite(store,database,callback=None):
    """Read-only import of a previous navigator cache. Jobs and settings are NOT copied."""
    database=Path(database).expanduser().resolve()
    if database==store.root/'catalog.sqlite':
        raise ValueError('Это текущая база веб-версии; повторный импорт не нужен.')
    source=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
    try:
        tables={r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'items','assets'}<=tables:raise ValueError('Это не база каталога Арктика-М.')
        records=[];assets=[]
        for (value,) in source.execute('SELECT data FROM items'):
            rec=json.loads(value)
            if rec.get('platform') in PLATFORMS:records.append(rec)
        for (value,) in source.execute('SELECT data FROM assets'):
            raw=json.loads(value)
            if raw.get('platform') not in PLATFORMS:continue
            a=normalize_asset(raw,{'source':'import'})
            if a:assets.append(a)
        store.upsert(records,assets)
        return {'files':1,'rows':len(records),'assets_processed':len(assets),
                'unique_assets':len(store.assets()),'unique_items':len(store.records())}
    finally:source.close()
