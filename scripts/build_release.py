#!/usr/bin/env python3
"""Воспроизводимый архив ровно проверенного Git-коммита; без локальных данных."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {'.git', '.venv', '.delivery', '__pycache__', 'node_modules',
                   'browser-results', 'runtime', 'downloads'}


def git(*args: str, cwd: Path = ROOT) -> bytes:
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True).stdout


def version_from_source(content: str) -> str:
    tree = ast.parse(content)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
            version = ast.literal_eval(node.value)
            if isinstance(version, str) and re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-z0-9.]+)?', version):
                return version
    raise ValueError('Не определена корректная версия __version__.')


def safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (not path.is_absolute() and '..' not in path.parts and '\\' not in name
            and not any(part in FORBIDDEN_PARTS for part in path.parts)
            and not path.name.startswith('.env')
            and path.name not in {'.netrc', '.cdsapirc', 'app.json'}
            and not name.endswith(('.sqlite', '.sqlite3', '.db', '.part', '.pyc')))


def build(root: Path, output: Path, commit: str = 'HEAD') -> dict:
    sha = git('rev-parse', '--verify', commit + '^{commit}', cwd=root).decode().strip()
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('Некорректный идентификатор коммита.')
    names = git('ls-tree', '-r', '--name-only', '-z', sha, cwd=root).decode().split('\0')
    names = [n for n in names if n]
    if any(not safe_name(n) for n in names):
        raise ValueError('В Git-дереве найдены локальные данные или технические файлы доставки.')
    def read(name: str) -> bytes:
        return git('show', sha + ':' + name, cwd=root)
    version = version_from_source(read('arktika/__init__.py').decode('utf-8'))
    manifest_bytes = read('MANIFEST.json')
    manifest = json.loads(manifest_bytes)
    if manifest['version'] != version:
        raise ValueError('Версии приложения и манифеста различаются.')
    bad = [n for n, h in manifest['files'].items()
           if not safe_name(n) or n not in names or hashlib.sha256(read(n)).hexdigest() != h]
    if bad:
        raise ValueError('Контрольные суммы не совпали: ' + ', '.join(bad))
    required = {'install.py', 'install.sh', 'install.cmd', 'start.sh', 'start.cmd',
                'server.py', 'static/index.html', 'docs/REANALYSIS.html', 'RELEASE_NOTES.md'}
    if not required.issubset(names) or not required.issubset(manifest['files']):
        raise ValueError('В поставке отсутствуют обязательные файлы.')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'arktika-worker-v{version}.zip'
    git('archive', '--format=zip', f'--prefix=arktika-worker-{version}/',
        f'--output={archive.resolve()}', sha, cwd=root)
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            raise ValueError('Архив не прошёл проверку CRC.')
        archived = {n[len(f'arktika-worker-{version}/'):] for n in z.namelist() if not n.endswith('/')}
        if archived != set(names):
            raise ValueError('Состав архива не совпадает с Git-коммитом.')
    info = {
        'version': version, 'tag': 'v' + version, 'commit': sha,
        'commit_time_utc': git('show', '-s', '--format=%cI', sha, cwd=root).decode().strip(),
        'source_files': len(names),
        'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'archive_bytes': archive.stat().st_size,
        'package': 'Исходники и установочные скрипты; зависимости загружаются при установке.',
        'verification': 'Технические проверки CI; не метеорологическая верификация и не живой вход CDS/Earthdata.',
    }
    info_path = output / 'build-info.json'
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    notes_path = output / 'RELEASE_NOTES.md'
    notes_path.write_bytes(read('RELEASE_NOTES.md'))
    files = [archive, info_path, notes_path]
    (output / 'SHA256SUMS.txt').write_text(''.join(
        hashlib.sha256(f.read_bytes()).hexdigest() + '  ' + f.name + '\n' for f in files), encoding='utf-8')
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--commit', default='HEAD')
    args = parser.parse_args()
    print(json.dumps(build(ROOT, args.output, args.commit), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
