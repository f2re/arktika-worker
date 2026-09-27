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
