"""Experimental spherical relative-vorticity diagnostic for geographic winds.

This module does not regrid data or detect a PMC. It derives only the horizontal
relative vorticity of a caller-attested 850-hPa geographic east/north wind field.
It supports regular axes and well-conditioned structured curvilinear lat/lon grids;
this does not by itself enable an archive adapter or validate provider metadata.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Any

import numpy as np
from .product_schema import product_passport

ALGORITHM_ID = "arktika.experimental_spherical_relative_vorticity"
ALGORITHM_VERSION = "0.2.0"
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


def _coordinates(latitude: Any, longitude: Any, shape: tuple[int, int]):
    lat = np.asarray(latitude, dtype=np.float64)
    lon = np.asarray(longitude, dtype=np.float64)
    if lat.ndim == lon.ndim == 1 and (lat.size, lon.size) == shape:
        lat1, lon1 = lat, lon
    elif lat.ndim == lon.ndim == 2 and lat.shape == lon.shape == shape:
        if not np.isfinite(lat).all() or not np.isfinite(lon).all():
            raise ValueError("latitude/longitude grid must be finite")
        rectilinear = (np.allclose(lat, lat[:, :1], rtol=0, atol=1e-8)
                       and np.allclose(lon, lon[:1, :], rtol=0, atol=1e-8))
        if rectilinear:
            lat1, lon1 = lat[:, 0], lon[0, :]
        else:
            if (not np.isfinite(lat).all() or not np.isfinite(lon).all()
                    or np.any(np.abs(lat) >= 87.0)):
                raise ValueError("curvilinear coordinates must be finite and remain below 87 degrees latitude")
            phi = np.deg2rad(lat)
            # Unwrap across columns and rows so row-specific 2π branches do not
            # create artificial cross-row longitude gradients at the dateline.
            lam = np.unwrap(np.unwrap(np.deg2rad(lon), axis=1), axis=0)
            if (np.max(np.abs(np.diff(lam, axis=0))) >= np.pi
                    or np.max(np.abs(np.diff(lam, axis=1))) >= np.pi):
                raise ValueError("curvilinear longitude mesh has an ambiguous dateline discontinuity")
            if np.max(np.hypot(np.diff(phi, axis=0),
                               np.cos(phi[:-1, :]) * np.diff(lam, axis=0))) > np.deg2rad(5.0):
                raise ValueError("curvilinear row spacing exceeds the supported local-grid limit")
            if np.max(np.hypot(np.diff(phi, axis=1),
                               np.cos(phi[:, :-1]) * np.diff(lam, axis=1))) > np.deg2rad(5.0):
                raise ValueError("curvilinear column spacing exceeds the supported local-grid limit")
            return phi, lam, "curvilinear_latitude_longitude"
    else:
        raise ValueError("coordinates must be matching 1-D axes or same-shape structured 2-D latitude/longitude meshes")
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
    lat2, lon2 = np.meshgrid(lat_rad, lon_rad, indexing="ij")
    return lat2, lon2, "regular_latitude_longitude"


def _curvilinear_derivatives(values: np.ndarray, phi: np.ndarray, lam: np.ndarray):
    """Convert row/column derivatives to geographic derivatives using the mesh Jacobian."""
    dphi_dr, dphi_dc = np.gradient(phi, axis=(0, 1), edge_order=2)
    dlam_dr, dlam_dc = np.gradient(lam, axis=(0, 1), edge_order=2)
    jacobian = dphi_dr * dlam_dc - dphi_dc * dlam_dr
    scale = np.abs(dphi_dr * dlam_dc) + np.abs(dphi_dc * dlam_dr)
    relative_area = np.divide(np.abs(jacobian), scale, out=np.zeros_like(jacobian), where=scale > 0)
    if (not np.isfinite(jacobian).all() or np.any(relative_area < 1e-5)
            or not (np.all(jacobian > 0) or np.all(jacobian < 0))):
        raise ValueError("curvilinear grid Jacobian is singular, ill-conditioned, or changes orientation")
    dvalue_dr, dvalue_dc = np.gradient(values, axis=(0, 1), edge_order=2)
    dvalue_dphi = (dvalue_dr * dlam_dc - dvalue_dc * dlam_dr) / jacobian
    dvalue_dlam = (dphi_dr * dvalue_dc - dphi_dc * dvalue_dr) / jacobian
    return dvalue_dphi, dvalue_dlam


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
    if field_metadata["source"] not in ("era5", "merra2", "carra2"):
        raise ValueError("wind source must be a declared ERA5, CARRA2, or MERRA-2 adapter")
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
    phi, lam, grid_type = _coordinates(latitude, longitude, eastward.shape)
    lat2, lon2 = phi, lam
    cos_lat = np.cos(phi)
    if grid_type == "regular_latitude_longitude":
        d_v_d_lon = np.gradient(northward, lam[0, :], axis=1, edge_order=2)
        d_u_cos_d_lat = np.gradient(eastward * cos_lat, phi[:, 0], axis=0, edge_order=2)
        zeta = (d_v_d_lon - d_u_cos_d_lat) / (EARTH_RADIUS_M * cos_lat)
    else:
        d_u_d_phi, _ = _curvilinear_derivatives(eastward, phi, lam)
        _, d_v_d_lam = _curvilinear_derivatives(northward, phi, lam)
        zeta = (d_v_d_lam - cos_lat * d_u_d_phi + eastward * np.sin(phi)) / (EARTH_RADIUS_M * cos_lat)
    zeta[~np.isfinite(zeta)] = np.nan

    coordinate_digest = hashlib.sha256(
        np.ascontiguousarray(np.stack((lat2, lon2), axis=0)).astype("<f8").tobytes()
    ).hexdigest()
    input_digest = hashlib.sha256(
        np.ascontiguousarray(np.stack((eastward, northward), axis=0)).astype("<f8").tobytes()
    ).hexdigest()
    grid = {"crs": "EPSG:4326", "grid_type": grid_type,
            "height": int(eastward.shape[0]), "width": int(eastward.shape[1]),
            "latitude_min_deg": float(np.rad2deg(phi).min()),
            "latitude_max_deg": float(np.rad2deg(phi).max()),
            "longitude_unwrapped_min_deg": float(np.rad2deg(lam).min()),
            "longitude_unwrapped_max_deg": float(np.rad2deg(lam).max()),
            "coordinate_sha256": coordinate_digest,
            "geolocation": "latitude_longitude_arrays", "grid_id": grid_id}
    source_record = {"source_id": field_metadata["source"],
                     "provider": field_metadata["source"],
                     "dataset_version": field_metadata["source_version"],
                     "field_id": field_metadata["field_id"],
                     "claimed_source_file_sha256": field_metadata["input_sha256"],
                     "claimed_file_sha256": field_metadata["input_sha256"],
                     "source_file_digest_attestation": "caller_supplied",
                     "file_digest_attestation": "caller_supplied; numerical function does not read or verify the cache file"}
    passport = product_passport(
        product_id="vorticity850:{}:{}:{}".format(
            field_metadata["source"], field_metadata["field_id"], timestamp),
        algorithm_id=ALGORITHM_ID,
        algorithm_version=ALGORITHM_VERSION,
        maturity="experimental",
        quantity_kind="atmospheric_dynamics",
        quantity="relative_vorticity",
        units="s-1",
        valid_time=timestamp,
        data_kind="analysis",
        grid=grid,
        source_grids=[{"crs": "EPSG:4326", "width": int(eastward.shape[1]),
                       "height": int(eastward.shape[0]), "grid_type": grid_type,
                       "coordinate_sha256": coordinate_digest,
                       "geolocation": "latitude_longitude_arrays", "grid_id": grid_id}],
        nodata="NaN; derivative stencils touching missing input remain missing",
        quality_control={"status": "input_contract_checked",
                         "rules": ["850-hPa instantaneous analysis", "wind in m s-1",
                                   "geographic east/north reference required",
                                   "nonfinite/nodata stencil propagation"]},
        sources=[source_record],
        calibration_version="not_applicable",
        calibration_status="not_applicable",
        limitations=[
            "Экспериментальная диагностика, не прогноз и не подтверждение ПМЦ.",
            "Хэш исходного файла передан вызывающим кодом; этот чистый численный модуль не подтверждает привязку файла к массивам.",
            "Криволинейная сетка поддерживается только как структурированная 2-D географическая mesh с невырожденным согласованным Jacobian; реальная геометрия CARRA2 здесь не проверялась.",
            "Полярные широты |latitude| >= 87° отклоняются из-за вырожденности регулярной lat/lon метрики.",
            "Эта функция только выводит поле вихревости; перепроецирование и детектор выполняются отдельно и не означают метеорологическую проверку.",
        ] + (["CARRA2 использует ERA5 в граничных условиях; их согласие не является независимым подтверждением."]
             if field_metadata["source"] == "carra2" else []),
    )
    passport.update({
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "maturity": "experimental",
        "units": "s-1",
        "valid_time": timestamp,
        "level_hPa": 850,
        "source": field_metadata["source"],
        "source_version": field_metadata["source_version"],
        "source_field_id": field_metadata["field_id"],
        "claimed_source_file_sha256": field_metadata["input_sha256"],
        "source_file_digest_attestation": "caller_supplied; this numerical function does not read or verify the cache file",
        "input_arrays_sha256": input_digest,
        "input": {"variable": "wind", "components": ["eastward_wind", "northward_wind"],
                  "units": "m s-1", "wind_reference": field_metadata["wind_reference"]},
        "method": {"equation": "(dv/dlambda - cos(phi)*du/dphi + u*sin(phi))/(a*cos(phi))",
                   "earth_radius_m": EARTH_RADIUS_M,
                   "derivative": ("second-order numpy.gradient on geographic coordinate radians"
                                  if grid_type == "regular_latitude_longitude"
                                  else "second-order row/column gradients transformed through the local spherical-coordinate Jacobian"),
                   "boundary": "second-order one-sided derivative",
                   "spatial_interpolation": "none"},
    })
    return {"values": zeta.astype(np.float32), "passport": passport}
