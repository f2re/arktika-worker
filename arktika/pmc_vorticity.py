"""Experimental spherical relative-vorticity diagnostic for regular lat/lon winds.

This module does not regrid data or detect a PMC. It derives only the horizontal
relative vorticity of a caller-attested 850-hPa geographic east/north wind field.
Curvilinear grids (including the currently normalized CARRA grid) are refused.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Any

import numpy as np

ALGORITHM_ID = "arktika.experimental_spherical_relative_vorticity"
ALGORITHM_VERSION = "0.1.0"
EARTH_RADIUS_M = 6_371_000.0


def _utc(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("valid_time must be an ISO-8601 UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("valid_time must be an ISO-8601 UTC string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("valid_time must include UTC offset +00:00")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _rectilinear_coordinates(latitude: Any, longitude: Any, shape: tuple[int, int]):
    lat = np.asarray(latitude, dtype=np.float64)
    lon = np.asarray(longitude, dtype=np.float64)
    if lat.ndim == lon.ndim == 1 and (lat.size, lon.size) == shape:
        lat1, lon1 = lat, lon
    elif lat.ndim == lon.ndim == 2 and lat.shape == lon.shape == shape:
        if not np.isfinite(lat).all() or not np.isfinite(lon).all():
            raise ValueError("latitude/longitude grid must be finite")
        lat1, lon1 = lat[:, 0], lon[0, :]
        if not np.allclose(lat, lat1[:, None], rtol=0, atol=1e-8):
            raise ValueError("curvilinear latitude grids are not supported")
        if not np.allclose(lon, lon1[None, :], rtol=0, atol=1e-8):
            raise ValueError("curvilinear longitude grids are not supported")
    else:
        raise ValueError("coordinates must be matching 1-D axes or a 2-D regular lat/lon mesh")
    if len(lat1) < 3 or len(lon1) < 3:
        raise ValueError("at least three rows and columns are required for centered derivatives")
    if not np.isfinite(lat1).all() or not np.isfinite(lon1).all():
        raise ValueError("latitude/longitude axes must be finite")
    if np.any(np.abs(lat1) >= 87.0):
        raise ValueError("grid too close to the pole for this regular lat/lon stencil")
    lat_rad = np.deg2rad(lat1)
    lon_rad = np.unwrap(np.deg2rad(lon1))
    if np.any(np.diff(lat_rad) == 0) or not (np.all(np.diff(lat_rad) > 0) or np.all(np.diff(lat_rad) < 0)):
        raise ValueError("latitude axis must be strictly monotonic")
    if np.any(np.diff(lon_rad) == 0) or not (np.all(np.diff(lon_rad) > 0) or np.all(np.diff(lon_rad) < 0)):
        raise ValueError("longitude axis must be strictly monotonic after dateline unwrapping")
    for axis, name in ((lat_rad, "latitude"), (lon_rad, "longitude")):
        spacing = np.diff(axis)
        if not np.allclose(spacing, spacing[0], rtol=1e-6, atol=1e-12):
            raise ValueError(f"{name} axis must have uniform spacing")
    # Refuse large gaps and global wrap ambiguity; this method is for a local native window.
    if np.max(np.abs(np.diff(lon_rad))) > np.deg2rad(5.0):
        raise ValueError("longitude spacing exceeds the supported local-window limit")
    return lat_rad, lon_rad


def derive_relative_vorticity_850(u: Any, v: Any, latitude: Any, longitude: Any, *,
                                  field_metadata: dict[str, Any],
                                  grid_id: str | None = None) -> dict[str, Any]:
    """Return ζ = dv/dx − du/dy using a spherical regular-lat/lon stencil.

    ``field_metadata`` must carry source, variable, units, level_hPa, valid_time,
    time_kind, wind_reference, and source_version. The returned passport covers
    the actual geographic axes and marks all nonfinite/nodata results explicitly.
    Derivatives touching missing input values remain missing; no values are filled.
    """
    if not isinstance(field_metadata, dict):
        raise ValueError("wind field passport is required")
    required = ("source", "field_id", "input_sha256", "variable", "units", "level_hPa",
                "valid_time", "time_kind", "wind_reference", "source_version")
    if any(key not in field_metadata for key in required):
        raise ValueError("wind field passport is incomplete")
    if field_metadata["source"] not in ("era5", "merra2"):
        raise ValueError("only declared regular-grid ERA5/MERRA-2 winds are supported")
    if field_metadata["variable"] != "wind" or field_metadata["units"] != "m s-1":
        raise ValueError("input must be 850-hPa horizontal wind in m s-1")
    if field_metadata["level_hPa"] != 850 or field_metadata["time_kind"] != "instantaneous_analysis":
        raise ValueError("input must be an instantaneous 850-hPa analysis")
    if field_metadata["wind_reference"] not in ("east_north", "geographic_from_direction"):
        raise ValueError("geographic east/north wind reference is not proven")
    if not isinstance(field_metadata["source_version"], str) or not field_metadata["source_version"].strip():
        raise ValueError("source dataset/version provenance is required")
    if not isinstance(field_metadata["field_id"], str) or not field_metadata["field_id"].strip():
        raise ValueError("source field identity is required")
    if not isinstance(field_metadata["input_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", field_metadata["input_sha256"]):
        raise ValueError("source field SHA-256 provenance is required")
    timestamp = _utc(field_metadata["valid_time"])
    # Preserve MaskedArray masks as NaN; never expose their underlying storage values.
    eastward = np.ma.asarray(u, dtype=np.float64).filled(np.nan)
    northward = np.ma.asarray(v, dtype=np.float64).filled(np.nan)
    if eastward.ndim != 2 or eastward.shape != northward.shape or min(eastward.shape) < 3:
        raise ValueError("u/v must be same-shape 2-D arrays, at least 3×3")
    if np.isinf(eastward).any() or np.isinf(northward).any():
        raise ValueError("infinite wind values are invalid; use NaN for nodata")
    lat_rad, lon_rad = _rectilinear_coordinates(latitude, longitude, eastward.shape)

    cos_lat = np.cos(lat_rad)[:, None]
    d_v_d_lon = np.gradient(northward, lon_rad, axis=1, edge_order=2)
    d_u_cos_d_lat = np.gradient(eastward * cos_lat, lat_rad, axis=0, edge_order=2)
    zeta = (d_v_d_lon - d_u_cos_d_lat) / (EARTH_RADIUS_M * cos_lat)
    zeta[~np.isfinite(zeta)] = np.nan

    lat2 = np.broadcast_to(lat_rad[:, None], eastward.shape)
    lon2 = np.broadcast_to(lon_rad[None, :], eastward.shape)
    coordinate_digest = hashlib.sha256(
        np.ascontiguousarray(np.stack((lat2, lon2), axis=0)).astype("<f8").tobytes()
    ).hexdigest()
    input_digest = hashlib.sha256(
        np.ascontiguousarray(np.stack((eastward, northward), axis=0)).astype("<f8").tobytes()
    ).hexdigest()
    passport = {
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "maturity": "experimental",
        "quantity": "relative_vorticity",
        "level_hPa": 850,
        "units": "s-1",
        "valid_time": timestamp,
        "source": field_metadata["source"],
        "source_version": field_metadata["source_version"],
        "source_field_id": field_metadata["field_id"],
        "claimed_source_file_sha256": field_metadata["input_sha256"],
        "source_file_digest_attestation": "caller_supplied; this numerical function does not read or verify the cache file",
        "input_arrays_sha256": input_digest,
        "input": {"variable": "wind", "components": ["eastward_wind", "northward_wind"],
                  "units": "m s-1", "wind_reference": field_metadata["wind_reference"]},
        "grid": {"crs": "EPSG:4326", "grid_type": "regular_latitude_longitude",
                 "height": int(eastward.shape[0]), "width": int(eastward.shape[1]),
                 "latitude_first_deg": float(np.rad2deg(lat_rad[0])),
                 "latitude_last_deg": float(np.rad2deg(lat_rad[-1])),
                 "longitude_unwrapped_first_deg": float(np.rad2deg(lon_rad[0])),
                 "longitude_unwrapped_last_deg": float(np.rad2deg(lon_rad[-1])),
                 "coordinate_sha256": coordinate_digest,
                 "grid_id": grid_id},
        "nodata": "NaN; derivative stencils touching missing input remain missing",
        "method": {"equation": "(dv/dlambda - d(u*cos(phi))/dphi)/(a*cos(phi))",
                   "earth_radius_m": EARTH_RADIUS_M,
                   "derivative": "second-order numpy.gradient on coordinate radians",
                   "boundary": "second-order one-sided derivative",
                   "spatial_interpolation": "none"},
        "limitations": [
            "Экспериментальная диагностика, не прогноз и не подтверждение ПМЦ.",
            "Хэш исходного файла передан вызывающим кодом; этот чистый численный модуль не подтверждает привязку файла к массивам.",
            "Поддерживается только регулярная географическая широта/долгота; CARRA и криволинейные сетки отклоняются.",
            "Полярные широты |latitude| >= 87° отклоняются из-за вырожденности регулярной lat/lon метрики.",
            "Полученное поле ещё не перепроецируется и не подключено к детектору кандидатов или пользовательскому сценарию.",
        ],
    }
    return {"values": zeta.astype(np.float32), "passport": passport}
