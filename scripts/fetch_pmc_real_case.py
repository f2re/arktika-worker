#!/usr/bin/env python3
"""Fetch an actual ERA5 case from the public Google Research ARCO archive.

The source objects are pinned by SHA-256. This script only selects existing
coordinates/levels, reorders longitudes, and writes lossless NetCDF subsets.
No meteorological values are generated or interpolated here. Requires the
application dependencies and numcodecs; no CDS or Google credentials are used.
The auxiliary profile converts real geopotential to geometric height using
the explicitly documented ECMWF spherical approximation.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
from pathlib import Path

import numpy as np
from numcodecs import get_codec
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import xarray as xr


BASE = 'https://storage.googleapis.com/gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3'
DOCUMENTATION = 'https://github.com/google-research/arco-era5'
HEIGHT_DOCUMENTATION = 'https://confluence.ecmwf.int/spaces/CKB/pages/158636068/ERA5%2Bcompute%2Bpressure%2Band%2Bgeopotential%2Bon%2Bmodel%2Blevels%2Bgeopotential%2Bheight%2Band%2Bgeometric%2Bheight'
EARTH_DOCUMENTATION = 'https://confluence.ecmwf.int/spaces/CKB/pages/76414402/ERA5%2Bdata%2Bdocumentation'
STAMP = '2020-01-01T00:00:00Z'
AREA = [85.0, -25.0, 60.0, 70.0]
TIME_INDEX = 1051896
OBJECTS = {
    'latitude': ('0', 844, 'ad2559188f8603a7a136eb4b81c1e96a4a78549a184b6e09def2a60a78631c25'),
    'longitude': ('0', 1587, '7942ebac63ba4e69e05575019aaebe2b2ebb927b316590cac1dcfc7eb08f09ee'),
    'level': ('0', 95, 'ac9e88edcb0fc6731acc62a7227d148972902796e519a0496381b5c599f686e8'),
    'time': ('15', 3577, '253cab9fea3ca06ae53e47ebb0b4bc23f8d94f50525b5730585e3a2e31f16daf'),
    'u_component_of_wind': ('1051896.0.0.0', 117184530, 'b52d511d4a2139b58a860c8bf7dfa5e283dafd02e4500620d9e0abd7ebb8c2cb'),
    'v_component_of_wind': ('1051896.0.0.0', 121384350, 'fbb08daf03264c29f66392e067bd9df4868a2e658d8d0479e4cbff02de24c3f0'),
    'temperature': ('1051896.0.0.0', 85235158, '0b68d74517cba580a5e67c88c1c3176cfae3957caf21f0aeb02ffbb29241a194'),
    'geopotential': ('1051896.0.0.0', 80830484, '43eafb03da11d316be05adc9695a198c5112b84ec80b2a8dbd7f355e353f4d9a'),
    'mean_sea_level_pressure': ('1051896.0.0', 2172546, '408833b3136695cc887efbf90bfe0ff72a93ce19d36323603749e3d94c1d629e'),
    '10m_u_component_of_wind': ('1051896.0.0', 3326458, 'b2f6c153de0b7f49a44f7ad0155c9e9b440ae8cb7925a34e6e634d8bfaa746c0'),
    '10m_v_component_of_wind': ('1051896.0.0', 3350918, '82ba0d45589c9823707daf9c3817b788753d4208968d6a1fb32d84e19213561a'),
    'sea_surface_temperature': ('1051896.0.0', 1384095, 'a83dffd5d78b3c8f6b6d65647f4d6b02dd1fd4ebe92cfd2833d3b6c2b630fbea'),
    'sea_ice_cover': ('1051896.0.0', 363015, 'af5ff8393f07a9e3e5d8a42e806e0cad5f303f3571eb3b03ed5da9822e584ee6'),
    'land_sea_mask': ('1051896.0.0', 808766, '03111ffe4a9da8dd3f2bf5201dc37cd5542e8c4c55cf3b220a863a7c201f98a3'),
}
PRESSURE = {'u_component_of_wind': 850, 'v_component_of_wind': 850, 'temperature': 500}
WIND10_CF = {
    '10m_u_component_of_wind': ('eastward_wind', 'https://codes.ecmwf.int/grib/param-db/165'),
    '10m_v_component_of_wind': ('northward_wind', 'https://codes.ecmwf.int/grib/param-db/166'),
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def session():
    client = requests.Session()
    retry = Retry(total=3, backoff_factor=.5, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=('GET',))
    client.mount('https://', HTTPAdapter(max_retries=retry))
    return client


def object_path(root, name):
    return root / 'upstream' / (name + '__' + OBJECTS[name][0] + '.bin')


def fetch_object(root, name):
    chunk, size, expected = OBJECTS[name]
    url = BASE + '/' + name + '/' + chunk
    path = object_path(root, name)
    cached = path.is_file() and path.stat().st_size == size and sha256(path) == expected
    headers = {}
    if not cached:
        temporary = path.with_suffix('.part')
        try:
            with session() as client, client.get(url, stream=True, timeout=(20, 90)) as response:
                response.raise_for_status()
                headers = {key: response.headers.get(key) for key in ('ETag', 'Last-Modified', 'x-goog-hash')}
                count = 0
                with temporary.open('wb') as stream:
                    for block in response.iter_content(1024 * 1024):
                        if not block:
                            continue
                        count += len(block)
                        if count > size:
                            raise ValueError('Source object exceeds its pinned size: ' + name)
                        stream.write(block)
            if count != size or sha256(temporary) != expected:
                raise ValueError('Source object differs from the reviewed SHA-256: ' + name)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    print(('VERIFIED ' if cached else 'DOWNLOADED ') + name + ' ' + str(size) + ' bytes', flush=True)
    return {'variable': name, 'url': url, 'chunk': chunk, 'bytes': size, 'sha256': expected,
            'path': str(path), 'cache_verified': cached, 'response_headers': headers}


def decode(root, metadata, name):
    spec = metadata[name + '/.zarray']
    expected_chunks = ([67706] if name == 'time' else [37] if name == 'level' else
                       [721] if name == 'latitude' else [1440] if name == 'longitude' else
                       [1, 37, 721, 1440] if name in PRESSURE or name == 'geopotential' else [1, 721, 1440])
    expected_dtype = '<i8' if name in ('level', 'time') else '<f4'
    if (spec.get('zarr_format') != 2 or spec.get('chunks') != expected_chunks
            or spec.get('dtype') != expected_dtype or spec.get('order') != 'C'
            or spec.get('filters') is not None or spec.get('compressor', {}).get('id') != 'blosc'):
        raise ValueError('Upstream array metadata changed: ' + name)
    raw = get_codec(spec['compressor']).decode(object_path(root, name).read_bytes())
    values = np.frombuffer(raw, dtype=expected_dtype)
    if values.size != int(np.prod(expected_chunks)):
        raise ValueError('Decoded chunk has an unexpected size: ' + name)
    return values.reshape(expected_chunks)


def attrs(metadata, name):
    return {key: value for key, value in metadata[name + '/.zattrs'].items() if key != '_ARRAY_DIMENSIONS'}


def extract(root, metadata):
    latitude = decode(root, metadata, 'latitude')
    longitude = decode(root, metadata, 'longitude')
    levels = decode(root, metadata, 'level')
    times = decode(root, metadata, 'time')
    if attrs(metadata, 'time').get('units') != 'hours since 1900-01-01 00:00:00':
        raise ValueError('Unexpected source time origin.')
    hours = int(times[TIME_INDEX - int(OBJECTS['time'][0]) * len(times)])
    instant = np.datetime64('1900-01-01T00:00:00') + np.timedelta64(hours, 'h')
    if hours != TIME_INDEX or instant != np.datetime64(STAMP.removesuffix('Z')):
        raise ValueError('Source time coordinate does not match the requested analysis.')
    if attrs(metadata, 'level').get('units') != 'Hectopascal(hPa)':
        raise ValueError('Unexpected source pressure-level units.')
    n, w, s, e = AREA
    lon_signed = (longitude.astype('float64') + 180) % 360 - 180
    yi = np.flatnonzero((latitude >= s) & (latitude <= n))
    xi = np.flatnonzero((lon_signed >= w) & (lon_signed <= e))
    xi = xi[np.argsort(lon_signed[xi])]
    if len(yi) != 101 or len(xi) != 381:
        raise ValueError('Unexpected size of the source grid subset.')
    coords = {
        'time': ('time', [instant], {'standard_name': 'time', 'long_name': 'analysis time, UTC'}),
        'latitude': ('latitude', latitude[yi], attrs(metadata, 'latitude')),
        'longitude': ('longitude', lon_signed[xi].astype('float32'), attrs(metadata, 'longitude')),
    }
    common = {
        'source': 'ECMWF ERA5 reanalysis; Google Research ARCO ERA5 curated public copy',
        'data_kind': 'analysis', 'title': 'ERA5 2020-01-01 00 UTC, Norwegian and Barents seas',
        'upstream_store': BASE, 'upstream_documentation': DOCUMENTATION,
        'history': 'Exact coordinate/level selection; longitudes relabelled and sorted; lossless NetCDF serialization. No interpolation.',
    }
    groups = {'wind850.nc': {}, 't500.nc': {}, 'surface.nc': {}}
    stats = {}
    for name in OBJECTS:
        if name in ('latitude', 'longitude', 'level', 'time', 'geopotential'):
            continue
        full = decode(root, metadata, name)
        attributes = attrs(metadata, name)
        if 'scale_factor' in attributes or 'add_offset' in attributes:
            raise ValueError('Unexpected encoded variable scaling: ' + name)
        if name in PRESSURE:
            matches = np.flatnonzero(levels == PRESSURE[name])
            if len(matches) != 1:
                raise ValueError('Requested pressure level is absent or ambiguous.')
            plane = full[0, int(matches[0])]
            selected = plane[np.ix_(yi, xi)]
            dims = ('time', 'pressure_level', 'latitude', 'longitude')
            values = selected[None, None, :, :]
            group = 't500.nc' if name == 'temperature' else 'wind850.nc'
        else:
            selected = full[0][np.ix_(yi, xi)]
            dims = ('time', 'latitude', 'longitude')
            values = selected[None, :, :]
            group = 'surface.nc'
        if name in WIND10_CF:
            standard, definition = WIND10_CF[name]
            if attributes.get('standard_name', standard) != standard:
                raise ValueError('Conflicting wind component metadata: ' + name)
            attributes.update(standard_name=standard, standard_name_reference=definition)
        groups[group][name] = (dims, values.copy(), attributes)
        finite = selected[np.isfinite(selected)]
        if not finite.size:
            raise ValueError('Requested region contains no valid values: ' + name)
        stats[name] = {'units': attributes.get('units'), 'finite_count': int(finite.size),
                       'missing_count': int(selected.size - finite.size),
                       'minimum': float(finite.min()), 'maximum': float(finite.max())}
        del full
    files = []
    for filename, variables in groups.items():
        coordinates = dict(coords)
        if filename != 'surface.nc':
            coordinates['pressure_level'] = ('pressure_level', [500 if filename == 't500.nc' else 850],
                                             {'units': 'hPa', 'standard_name': 'air_pressure',
                                              'original_units': 'Hectopascal(hPa)'})
        dataset = xr.Dataset(variables, coords=coordinates, attrs=common)
        path = root / filename
        temporary = path.with_suffix('.part.nc')
        encoding = {name: {'zlib': True, 'complevel': 4, 'dtype': 'float32', '_FillValue': np.float32(np.nan)} for name in variables}
        encoding['time'] = {'units': 'hours since 1900-01-01 00:00:00', 'calendar': 'proleptic_gregorian', 'dtype': 'int64'}
        dataset.to_netcdf(temporary, engine='netcdf4', encoding=encoding)
        temporary.replace(path)
        dataset.close()
        files.append({'filename': filename, 'path': str(path), 'bytes': path.stat().st_size, 'sha256': sha256(path)})
    return files, stats


def extract_profile(root, metadata):
    latitude = decode(root, metadata, 'latitude')
    longitude = decode(root, metadata, 'longitude')
    levels = decode(root, metadata, 'level')
    yi = np.flatnonzero(latitude == 70.0)
    xi = np.flatnonzero(longitude == 30.0)
    if len(yi) != 1 or len(xi) != 1:
        raise ValueError('The requested profile point is not a unique source grid node.')
    selected = np.flatnonzero((levels >= 100) & (levels <= 850))[::-1]
    if attrs(metadata, 'temperature').get('units') != 'K' or attrs(metadata, 'geopotential').get('units') != 'm**2 s**-2':
        raise ValueError('Unexpected temperature/geopotential profile units.')
    temperature = decode(root, metadata, 'temperature')[0, selected, int(yi[0]), int(xi[0])].astype('float64')
    geopotential = decode(root, metadata, 'geopotential')[0, selected, int(yi[0]), int(xi[0])].astype('float64')
    gravity, radius = 9.80665, 6371229.0
    geopotential_height = geopotential / gravity
    height = radius * geopotential_height / (radius - geopotential_height)
    if (not np.isfinite(temperature).all() or not np.isfinite(height).all()
            or not np.all(np.diff(height) > 0) or not np.all((height >= -500) & (height <= 35000))):
        raise ValueError('The real profile does not satisfy the import geometry contract.')
    profile = {
        'data_kind': 'analysis', 'source': 'ECMWF ERA5 / ARCO; 850–100 гПа. Геометрическая высота: сферическое приближение ECMWF из геопотенциала; горизонтальные изменения g не учтены.',
        'valid_time': STAMP, 'lat': 70.0, 'lon': 30.0,
        'height_units': 'm', 'temperature_units': 'K', 'pressure_units': 'Pa', 'vertical_reference': 'AMSL',
        'height': height.tolist(), 'temperature': temperature.tolist(),
        'pressure': (levels[selected].astype('float64') * 100).tolist(),
        'radius_km': 1, 'max_hours': .1,
        'source_geopotential': geopotential.tolist(), 'source_geopotential_units': 'm2 s-2',
        'geopotential_height_m': geopotential_height.tolist(),
        'height_conversion': {'formula': 'h=geopotential/g0; altitude=Re*h/(Re-h)', 'g0_m_s2': gravity,
                              'earth_radius_m': radius, 'approximation': 'Spherical Earth; horizontal gravity variation neglected; MSL approximates the geoid.',
                              'references': [HEIGHT_DOCUMENTATION, EARTH_DOCUMENTATION]},
        'note': 'Real reanalysis point profile; only original 850–100 hPa levels selected. Radius and time tolerance are conservative application settings, not estimated representativeness errors.',
    }
    path = root / 'profile.json'
    temporary = path.with_suffix('.part.json')
    temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
    return {'filename': path.name, 'path': str(path), 'bytes': path.stat().st_size, 'sha256': sha256(path),
            'valid_time': STAMP, 'lat': 70.0, 'lon': 30.0, 'level_count': int(len(selected)),
            'height_conversion': profile['height_conversion']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Directory outside the git repository for real data and provenance.')
    args = parser.parse_args()
    root = args.output.expanduser().resolve()
    repository = Path(__file__).resolve().parents[1]
    if root == repository or repository in root.parents:
        raise ValueError('Keep the real data outside the git repository; choose another --output directory.')
    root.mkdir(parents=True, exist_ok=True)
    (root / 'upstream').mkdir(exist_ok=True)
    with session() as client:
        response = client.get(BASE + '/.zmetadata', timeout=(20, 60))
        response.raise_for_status()
        metadata_bytes = response.content
    if len(metadata_bytes) > 2 * 1024 * 1024:
        raise ValueError('Unexpectedly large source metadata.')
    (root / 'source_zmetadata.json').write_bytes(metadata_bytes)
    metadata = json.loads(metadata_bytes)['metadata']
    with ThreadPoolExecutor(max_workers=3) as executor:
        objects = list(executor.map(lambda name: fetch_object(root, name), OBJECTS))
    files, stats = extract(root, metadata)
    profile = extract_profile(root, metadata)
    files.append(profile)
    by_name = {row['filename']: row for row in files}
    selections = [('wind', 850, 'wind850.nc'), ('mslp', None, 'surface.nc'), ('wind10', None, 'surface.nc'),
                  ('t', 500, 't500.nc'), ('sst', None, 'surface.nc'), ('ice', None, 'surface.nc'), ('lsm', None, 'surface.nc')]
    fields = [{'source': 'era5', 'variable': variable, 'level': level, 'time': STAMP, 'area': AREA,
               'path': by_name[filename]['path'], 'sha256': by_name[filename]['sha256']}
              for variable, level, filename in selections]
    provenance = {
        'schema_version': 'arktika.real_reanalysis_case.v1', 'data_kind': 'analysis', 'source': 'era5',
        'time': STAMP, 'valid_time': STAMP, 'area': AREA,
        'verified_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'upstream': {'url': BASE, 'documentation_url': DOCUMENTATION,
                     'metadata_url': BASE + '/.zmetadata', 'metadata_sha256': hashlib.sha256(metadata_bytes).hexdigest(),
                     'store_attributes': metadata.get('.zattrs', {})},
        'upstream_objects': objects, 'files': files, 'fields': fields, 'statistics': stats, 'profile': profile,
        'original_variable_attributes': {name: metadata[name + '/.zattrs'] for name in OBJECTS},
        'processing': ['Selected the exact original 2020-01-01 00 UTC time and 850/500 hPa coordinates.',
                       'Selected original grid nodes; relabelled longitude 0..360 as -180..180 and reordered columns.',
                       'Renamed pressure units Hectopascal(hPa) to equivalent CF hPa without changing pressure values.',
                       'Added CF standard_name to 10m u/v from the documented ECMWF parameter definitions.',
                       'Serialized source float32 values and missing values losslessly; no interpolation or invented fields.',
                       'Extracted real 850–100 hPa T/geopotential at 70N30E and converted height using the documented ECMWF spherical approximation.'],
        'metadata_references': {name: definition for name, (_, definition) in WIND10_CF.items()},
        'limitations': ['Public curated ERA5 copy already regridded upstream to 0.25 degrees; not raw native-grid GRIB.',
                        'A single real analysis is an integration check, not independent validation of polar-low detection.',
                        'The auxiliary profile uses approximate geometric AMSL height, ignoring horizontal gravity variation; no standard atmosphere is used.',
                        'No CDS-authenticated download pathway or independent cyclone catalogue was verified by this fetch.'],
    }
    temporary = root / 'provenance.part.json'
    temporary.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(root / 'provenance.json')
    print(json.dumps({'source': 'era5', 'time': STAMP, 'area': AREA, 'files': files,
                      'provenance': str(root / 'provenance.json')}, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
