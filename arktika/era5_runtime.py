"""Локальное окружение ERA5: установка по действию пользователя, не в системный Python."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from .download import atomic_json, digest
from .era5_access import cancelled

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ('numpy', 'rasterio', 'pyproj', 'PIL', 'xarray', 'netCDF4', 'cdsapi', 'requests')
_LOCK = threading.Lock()
_CACHE = {}
PROBE = '''import importlib,json
missing=[];versions={}
for name in %r:
 try:
  m=importlib.import_module(name);versions[name]=str(getattr(m,'__version__','installed'))
 except Exception: missing.append(name)
engine=False
try:
 import pyrttov
 r=pyrttov.Rttov()
 engine=callable(r.loadInst) and hasattr(pyrttov,'Profiles')
except Exception: pass
print(json.dumps(dict(missing=missing,versions=versions,pyrttov=engine)))
''' % (PACKAGES,)


def python_path(folder):
    return Path(folder) / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def process_options():
    return {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}


def stop_process(proc):
    if proc.poll() is not None:
        return
    if os.name != 'nt':
        try: os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError: return
    else:
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    try: proc.wait(3)
    except subprocess.TimeoutExpired:
        if os.name != 'nt':
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        else: proc.kill()
        proc.wait()


def run_process(args, cancel=None, timeout=900, env=None, tick=lambda: None, payload=None):
    """Fixed argv only; output may contain credentials from libraries and is never logged."""
    cancelled(cancel)
    proc = subprocess.Popen([str(a) for a in args], stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, **process_options())
    try:
        if payload is not None:
            # Payload is bounded by the caller and never appears in argv or a temporary file.
            proc.stdin.write(json.dumps(payload).encode()); proc.stdin.close()
        until = time.monotonic() + timeout
        while proc.poll() is None:
            cancelled(cancel)
            if time.monotonic() > until: raise ValueError('Истекло время подготовки. Повторный запуск использует завершённые файлы.')
            tick(); time.sleep(.2)
        cancelled(cancel)
        return proc.returncode
    finally:
        stop_process(proc)


def engine_directory(root):
    configured = os.environ.get('ARKTIKA_RTTOV_PATH')
    if not configured:
        try: configured = json.loads((Path(root)/'era5'/'engine.json').read_text())['wrapper']
        except (OSError, ValueError, KeyError): pass
    candidates = [Path(configured).expanduser()] if configured else [ROOT/'rttov132'/'wrapper', Path.home()/'rttov132'/'wrapper', Path('/opt/rttov132/wrapper')]
    return next((p.resolve() for p in candidates if (p/'pyrttov').is_dir() or (p/'pyrttov.py').is_file()), None)


def runtime_env(root):
    env = os.environ.copy()
    wrapper = engine_directory(root)
    if wrapper:
        paths = [str(wrapper)]
        lib = wrapper.parent/'lib'
        if lib.is_dir(): paths.append(str(lib))
        env['PYTHONPATH'] = os.pathsep.join(paths + ([env['PYTHONPATH']] if env.get('PYTHONPATH') else []))
    return env


def probe(executable, root, refresh=False):
    exe = str(executable); env = runtime_env(root)
    key = (exe, env.get('PYTHONPATH', ''))
    previous = _CACHE.get(key)
    if not refresh and previous and time.monotonic()-previous[0] < 15:
        return dict(previous[1])
    failed = {'missing': list(PACKAGES), 'versions': {}, 'pyrttov': False}
    try:
        p = subprocess.run([exe, '-c', PROBE], capture_output=True, timeout=20, env=env)
        info = json.loads(p.stdout.splitlines()[-1]) if p.returncode == 0 else failed
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired): info = failed
    _CACHE[key] = (time.monotonic(), info)
    return dict(info)


def runtime_info(root, refresh=False):
    managed = python_path(Path(root)/'era5'/'runtime')
    candidates = [managed, Path(sys.executable)] if managed.is_file() else [Path(sys.executable)]
    best = None
    for exe in candidates:
        info = probe(exe, root, refresh)
        candidate = dict(info, python=str(exe), managed=exe == managed)
        if best is None or len(info['missing']) < len(best['missing']): best = candidate
        if not info['missing']: return candidate
    return best


def ensure_runtime(root, cancel=None, progress=lambda message: None):
    # App.start serialises jobs; this lock also protects direct setup callers.
    while not _LOCK.acquire(timeout=.2): cancelled(cancel)
    try:
        info = runtime_info(root, refresh=True)
        if not info['missing']: return info
        folder = Path(root)/'era5'/'runtime'; exe = python_path(folder)
        progress('Устанавливаю библиотеки ERA5 в отдельное окружение приложения. Системный Python не изменяется.')
        if not exe.is_file():
            code = run_process([sys.executable, '-m', 'venv', '--system-site-packages', folder], cancel)
            if code: raise ValueError('Не удалось создать окружение ERA5. Нужен модуль venv этого Python и доступ на запись в папку состояния.')
        env = runtime_env(root)
        # No private indexes/URLs from environment or pip.conf, no source builds or elevation.
        env = {k:v for k,v in env.items() if not k.startswith('PIP_')}
        env['PIP_CONFIG_FILE'] = os.devnull
        args = [exe, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--only-binary=:all:',
                '--timeout', '30', '--retries', '2', '-r', ROOT/'requirements.txt', '-r', ROOT/'requirements-era5.txt']
        wheelhouse = ROOT/'wheelhouse'
        args += ['--no-index', '--find-links', str(wheelhouse)] if wheelhouse.is_dir() else ['--index-url', 'https://pypi.org/simple']
        if run_process(args, cancel, env=env):
            raise ValueError('Библиотеки ERA5 не установлены. Проверьте доступ к PyPI, свободное место и наличие колёс для этого Python. Для офлайн-установки положите колёса в wheelhouse и нажмите «Повторить».')
        result = dict(probe(exe, root, refresh=True), python=str(exe), managed=True)
        if result['missing']: raise ValueError('Проверка импорта после установки не пройдена: '+', '.join(result['missing']))
        atomic_json(folder/'ready.json', {'requirements_sha256':digest(ROOT/'requirements-era5.txt'), 'versions':result['versions']})
        progress('Библиотеки ERA5 установлены и проверены. Перезапуск сервера не нужен.')
        return result
    finally: _LOCK.release()


def configure_engine(root, value):
    if not isinstance(value,str) or not value.strip() or len(value)>4096: raise ValueError('Укажите папку установленного RTTOV 13.2.')
    path = Path(value).expanduser().resolve()
    candidates = [path, path/'wrapper']
    wrapper = next((p for p in candidates if (p/'pyrttov').is_dir() or (p/'pyrttov.py').is_file()),None)
    if wrapper is None: raise ValueError('В этой папке нет обёртки pyrttov. Выберите RTTOV с собранной Python-обёрткой.')
    (Path(root)/'era5').mkdir(parents=True,exist_ok=True)
    atomic_json(Path(root)/'era5'/'engine.json', {'wrapper':str(wrapper)})
    info = runtime_info(root, refresh=True)
    return {'pyrttov':info['pyrttov'], 'message':'Обёртка RTTOV загружается.' if info['pyrttov'] else 'Папка сохранена, но обёртка не загружается. Проверьте сборку и совместимость Python по инструкции.'}
