#!/usr/bin/env python3
"""Тот же установщик, что запускается кнопкой ERA5; для диагностики и CI."""
import argparse
import json
from pathlib import Path
import sys
import threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from arktika.era5_runtime import ensure_runtime


def main():
    # Redirected Windows output may default to cp1252; Russian progress must not abort setup.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser=argparse.ArgumentParser()
    parser.add_argument('--state-dir',required=True,help='Папка состояния приложения')
    args=parser.parse_args()
    state=Path(args.state_dir).expanduser().resolve();state.mkdir(parents=True,exist_ok=True)
    runtime=ensure_runtime(state,threading.Event(),print)
    # Path is local CLI diagnostics, not a public HTTP response. Never prints secrets.
    print(json.dumps({'managed':runtime['managed'],'python':runtime['python'],
                      'missing':runtime['missing'],'versions':runtime['versions'],'pyrttov':runtime['pyrttov']},ensure_ascii=False,indent=2))
    if runtime['missing']:raise SystemExit(1)

if __name__=='__main__':main()
