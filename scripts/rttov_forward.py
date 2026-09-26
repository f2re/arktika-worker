#!/usr/bin/env python3
"""Запуск установленного RTTOV 13.2. Нет искусственных выходов при ошибке."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from arktika.era5_forward import simulate_profiles
from arktika.download import atomic_json
if __name__=='__main__':
    try:
        inp=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
        values=simulate_profiles(inp['profiles'],inp['channels'],inp['coefficient'])
        atomic_json(Path(sys.argv[2]),{'brightness_temperature_k':values.tolist()})
    except Exception:
        sys.exit(1)
