#!/usr/bin/env python3
"""Live public NWP SAF download + cache reuse. No ERA5 account or RTTOV needed."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from arktika.era5_forward import download_coefficients, coefficient_info


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='coefficient-check.json')
    args=parser.parse_args()
    report={'kind':'live-official-coefficient-download','pyrttov_present':bool(importlib.util.find_spec('pyrttov'))}
    try:
        with tempfile.TemporaryDirectory() as folder:
            first=download_coefficients(folder,progress=lambda m:print(m,flush=True))
            if not first.get('present') or first.get('origin')!='official_https_download':
                raise AssertionError('Official table not validated')
            with patch('urllib.request.build_opener',side_effect=AssertionError('Cache must not use network')):
                second=download_coefficients(folder)
            assert first['sha256']==second['sha256']==coefficient_info(folder)['sha256']
            report.update(status='passed',coefficient=first,cache_reused_without_network=True)
    except Exception as exc:
        report.update(status='failed',error=str(exc))
        raise
    finally:
        Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
