#!/usr/bin/env python3
"""Установщик в локальное окружение .venv; системный Python не изменяется."""
import argparse,os,subprocess,sys,venv
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def main():
 if sys.version_info<(3,10):raise SystemExit('Нужен Python 3.10 или новее; рекомендуется 3.11/3.12.')
 p=argparse.ArgumentParser();p.add_argument('--wheelhouse');p.add_argument('--minimal',action='store_true',help='Только просмотр GeoTIFF, без библиотек ERA5');a=p.parse_args()
 env=ROOT/'.venv';venv.EnvBuilder(with_pip=True).create(env)
 exe=env/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
 cmd=[str(exe),'-m','pip','install','--only-binary=:all:','-r',str(ROOT/'requirements.txt')]
 if not a.minimal:cmd+=['-r',str(ROOT/'requirements-era5.txt')]
 if a.wheelhouse:cmd+=['--no-index','--find-links',str(Path(a.wheelhouse).expanduser().resolve())]
 subprocess.run(cmd,check=True)
 subprocess.run([str(exe),str(ROOT/'check.py')],check=True,cwd=ROOT)
 print('Установка завершена. Windows: start.cmd; Linux/macOS: sh start.sh')
if __name__=='__main__':main()
