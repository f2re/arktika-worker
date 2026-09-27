#!/usr/bin/env python3
"""ERA5 в отдельном окружении. Секреты только stdin, на диск — публичный прогресс."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from arktika.download import atomic_json
from arktika.era5_calculation import calculate_reference

if __name__ == '__main__':
    payload=json.load(sys.stdin)
    folder=Path(payload['root'])/'era5'/'runs'/payload['identity']
    def progress(message):
        atomic_json(folder/'progress.json',{'message':message})
    calculate_reference(payload['scene'],payload['plan'],payload['options'],payload['root'],
                        payload['credential'],payload['identity'],progress=progress)
