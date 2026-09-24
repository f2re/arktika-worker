#!/usr/bin/env python3
"""Быстрая проверка зависимостей и EPSG, без обращения к сети."""
import sys
from importlib.metadata import version

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if sys.version_info < (3, 10):
    raise SystemExit("Нужен Python 3.10+.")

from arktika.geo import GEOD, grid
import rasterio  # noqa: F401

for package in ("numpy", "rasterio", "pyproj", "Pillow"):
    print(package, version(package))

g = grid("arctic", 256)
assert g["crs"] == "EPSG:3413"
assert GEOD.inv(0, 0, 1, 0)[2] > 111000
print("Базовая проверка завершена. Это не проверка доступа GPTL или метеорологической точности.")
