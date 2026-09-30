#!/usr/bin/env python3
"""Фиксированная точка входа изолированного обработчика реанализа."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from arktika.reanalysis.worker import main
if __name__=='__main__': main()
