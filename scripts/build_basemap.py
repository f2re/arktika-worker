#!/usr/bin/env python3
"""Reproduce the bundled coarse coastline, never operational boundary data.

Build-only dependencies: pyogrio==0.12.1 and shapely==2.1.2.
The application uses the resulting GeoJSON and needs neither build package.
"""
import hashlib
import json
from pathlib import Path


def main():
    import pyogrio
    import shapely
    from pyogrio.raw import read
    from shapely.geometry import mapping
    root = Path(__file__).resolve().parents[1]
    fixture = Path(pyogrio.__file__).parent / 'tests/fixtures/naturalearth_lowres/naturalearth_lowres.shp'
    expected = '08e341606e8391e458c3f08deb312de664b56bfae376064c5aa0aee6681a5f55'
    if hashlib.sha256(fixture.read_bytes()).hexdigest() != expected:
        raise SystemExit('Unexpected Natural Earth fixture version')
    geometry = mapping(shapely.union_all(shapely.from_wkb(read(fixture)[2])))
    data = dict(type='Feature', properties=dict(source='Natural Earth lowres, pyogrio fixture, dissolved',
                license='Public domain', purpose='Обзорная подложка, не навигационная карта'), geometry=geometry)
    raw = json.dumps(data, separators=(',', ':')).encode('utf-8')
    if hashlib.sha256(raw).hexdigest() != 'cf754ca838b69cc3231777ec2842facc86f5d13e9273202ba03e7df581659acf':
        raise SystemExit('Coastline reconstruction differs; retain the existing file and inspect the build stack')
    (root / 'static/land.geojson').write_bytes(raw)
    print('Verified coarse coastline:', len(raw), 'bytes')


if __name__ == '__main__':
    main()
