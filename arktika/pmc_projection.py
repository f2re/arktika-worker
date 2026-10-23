"""Experimental reprojection of 850 hPa vorticity from geographic grids.

This module only maps an already-derived scalar field onto a caller-selected
projected grid. It does not validate projection scale suitability or connect to
the PMC detector; no meteorological verification is implied.
"""
from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
from affine import Affine
from pyproj import CRS, Proj
from rasterio.enums import Resampling
from rasterio.warp import reproject

from .pmc_vorticity import _coordinates
from .product_schema import product_passport

MAX_DETECTOR_PROJECTION_SCALE_ERROR = 0.05


def _axis(values: Any, name: str, expected: int) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim == 1:
        axis = arr
    elif arr.ndim == 2:
        if name == "latitude":
            if not np.allclose(arr, arr[:, :1], rtol=0, atol=1e-8):
                raise ValueError("curvilinear latitude/longitude meshes are unsupported")
            axis = arr[:, 0]
        else:
            if not np.allclose(arr, arr[:1, :], rtol=0, atol=1e-8):
                raise ValueError("curvilinear latitude/longitude meshes are unsupported")
            axis = arr[0, :]
    else:
        raise ValueError(f"{name} must be a 1-D axis or rectilinear 2-D mesh")
    if axis.size != expected or axis.size < 2 or not np.isfinite(axis).all():
        raise ValueError(f"{name} axis dimensions or values are invalid")
    if name == "longitude":
        # Unwrap local dateline crossings, but do not silently bridge a true gap.
        axis = np.rad2deg(np.unwrap(np.deg2rad(axis)))
        step = np.diff(axis)
        if np.all(step < 0):
            pass
        elif not np.all(step > 0):
            raise ValueError("longitude axis must be strictly monotone after dateline unwrapping")
    else:
        step = np.diff(axis)
        if not (np.all(step > 0) or np.all(step < 0)):
            raise ValueError("latitude axis must be strictly monotone")
        if np.any(np.abs(axis) > 90):
            raise ValueError("latitude values must lie between -90 and 90 degrees")
    delta = np.diff(axis)
    spacing = float(np.median(delta))
    if not np.allclose(delta, spacing, rtol=1e-6, atol=max(1e-10, abs(spacing) * 1e-8)):
        raise ValueError(f"{name} axis must have uniform spacing; gaps are not filled")
    if name == "longitude" and abs(spacing) > 5.0:
        raise ValueError("longitude spacing exceeds supported local-grid limit")
    return axis


def _source_grid(values: np.ndarray, latitude: Any, longitude: Any) -> tuple[Affine, str, np.ndarray, np.ndarray]:
    lat = _axis(latitude, "latitude", values.shape[0])
    lon = _axis(longitude, "longitude", values.shape[1])
    dx = float(np.median(np.diff(lon)))
    dy = float(np.median(np.diff(lat)))
    transform = Affine(dx, 0, float(lon[0] - dx / 2), 0, dy, float(lat[0] - dy / 2))
    lat2, lon2 = np.meshgrid(np.deg2rad(lat), np.deg2rad(lon), indexing="ij")
    digest = hashlib.sha256(np.ascontiguousarray(np.stack((lat2, lon2)).astype("<f8")).tobytes()).hexdigest()
    return transform, digest, lat, lon


def _projection_scale(latitudes: np.ndarray, longitudes: np.ndarray, crs: CRS) -> dict[str, float]:
    projection = Proj(crs)
    lat_samples = np.linspace(float(np.min(latitudes)), float(np.max(latitudes)), min(5, latitudes.size))
    lon_samples = np.linspace(float(np.min(longitudes)), float(np.max(longitudes)), min(5, longitudes.size))
    errors = []
    for lat in lat_samples:
        for lon in lon_samples:
            factors = projection.get_factors(float(lon), float(lat))
            errors.extend((abs(float(factors.meridional_scale) - 1.0),
                           abs(float(factors.parallel_scale) - 1.0)))
    if not np.isfinite(errors).all():
        raise ValueError("target projection scale could not be evaluated")
    return {"maximum_relative_error": float(max(errors, default=0.0)),
            "sample_count": int(len(errors) // 2)}


def _target_grid(target_grid: dict[str, Any], shape: tuple[int, int]) -> tuple[dict[str, Any], Affine, CRS]:
    if not isinstance(target_grid, dict):
        raise ValueError("explicit target_grid mapping is required")
    required = ("crs", "width", "height", "transform", "resolution_x_m", "resolution_y_m")
    if any(key not in target_grid for key in required):
        raise ValueError("target grid requires crs, width, height, transform and metric resolutions")
    try:
        crs = CRS.from_user_input(target_grid["crs"])
        width_value, height_value = target_grid["width"], target_grid["height"]
        if (isinstance(width_value, bool) or isinstance(height_value, bool)
                or not isinstance(width_value, int) or not isinstance(height_value, int)):
            raise ValueError
        width, height = width_value, height_value
        raw = target_grid["transform"]
        if isinstance(raw, Affine):
            transform = raw
        else:
            if len(raw) != 6:
                raise ValueError
            transform = Affine(*[float(v) for v in raw])
        dx, dy = float(target_grid["resolution_x_m"]), float(target_grid["resolution_y_m"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("target grid metadata is invalid") from exc
    if not crs.is_projected or not crs.axis_info or any(
            abs(float(axis.unit_conversion_factor or 0) - 1.0) > 1e-9 for axis in crs.axis_info[:2]):
        raise ValueError("target CRS must be projected with metre axes")
    if width < 1 or height < 1 or (height, width) != shape:
        raise ValueError("target grid dimensions must match target array dimensions")
    coeff = (transform.a, transform.b, transform.c, transform.d, transform.e, transform.f)
    if not np.isfinite(coeff).all() or transform.a == 0 or transform.e == 0:
        raise ValueError("target affine transform must be finite and nonsingular")
    if abs(transform.b) > 1e-12 or abs(transform.d) > 1e-12:
        raise ValueError("rotated target transforms are unsupported")
    if dx <= 0 or dy <= 0 or not np.isclose(abs(transform.a), dx) or not np.isclose(abs(transform.e), dy):
        raise ValueError("target metric resolutions must match affine pixel sizes")
    normalized = {"crs": crs.to_string(), "width": width, "height": height,
                  "resolution_x_m": dx, "resolution_y_m": dy,
                  "transform": [float(v) for v in coeff]}
    return normalized, transform, crs


def reproject_vorticity_to_grid(values: Any, latitude: Any, longitude: Any, *,
                                target_grid: dict[str, Any],
                                source_passport: dict[str, Any] | None = None) -> dict[str, Any]:
    """Warp vorticity from regular geographic axes to an explicit metric grid.

    Values are bilinearly reprojected, while a nearest-neighbour validity warp
    prevents output where the nearest source sample is nodata. Missing samples
    are never filled. ``source_passport`` is preserved, with the original grid
    copied to ``source_grid`` and the destination grid installed as ``grid``.
    """
    source = np.ma.asarray(values, dtype=np.float32).filled(np.nan)
    if source.ndim != 2 or min(source.shape) < 2 or np.isinf(source).any():
        raise ValueError("source vorticity must be a finite-or-NaN 2-D array")
    lat_values = np.asarray(latitude, dtype=np.float64)
    lon_values = np.asarray(longitude, dtype=np.float64)
    phi, lam, coordinate_grid_type = _coordinates(lat_values, lon_values, source.shape)
    coord_digest = hashlib.sha256(
        np.ascontiguousarray(np.stack((phi, lam), axis=0)).astype("<f8").tobytes()
    ).hexdigest()
    rectilinear = (lat_values.ndim == lon_values.ndim == 1 or
                   (lat_values.ndim == lon_values.ndim == 2
                    and lat_values.shape == lon_values.shape == source.shape
                    and np.allclose(lat_values, lat_values[:, :1], rtol=0, atol=1e-8)
                    and np.allclose(lon_values, lon_values[:1, :], rtol=0, atol=1e-8)))
    if rectilinear:
        source_transform, _, lat_axis, lon_axis = _source_grid(
            source, lat_values, lon_values)
        lat_mesh, lon_mesh = np.meshgrid(lat_axis, lon_axis, indexing="ij")
        grid_type = "regular_latitude_longitude"
    else:
        grid_type = coordinate_grid_type
        if grid_type != "curvilinear_latitude_longitude":
            raise ValueError("source coordinates could not be identified as rectilinear or curvilinear")
        lat_mesh, lon_mesh = np.rad2deg(phi), np.rad2deg(lam)
        source_transform = None
    try:
        requested_shape = (int(target_grid["height"]), int(target_grid["width"]))
    except (TypeError, KeyError, ValueError) as exc:
        raise ValueError("target grid dimensions are required") from exc
    grid, target_transform, target_crs = _target_grid(target_grid, requested_shape)
    required_provenance = ("source", "valid_time", "algorithm_id", "algorithm_version")
    if (not isinstance(source_passport, dict)
            or any(not source_passport.get(key) for key in required_provenance)
            or not isinstance(source_passport.get("grid"), dict)):
        raise ValueError("complete source passport is required for reprojected vorticity")
    attested_coordinate_digest = source_passport["grid"].get("coordinate_sha256")
    if not isinstance(attested_coordinate_digest, str) or attested_coordinate_digest != coord_digest:
        raise ValueError("source coordinate arrays do not match the vorticity passport digest")
    source_grid = dict(source_passport["grid"])
    source_grid.update({"crs": "EPSG:4326", "width": int(source.shape[1]),
                        "height": int(source.shape[0]), "coordinate_sha256": coord_digest,
                        "grid_type": grid_type})
    if source_transform is not None:
        source_grid["transform"] = [float(v) for v in (source_transform.a, source_transform.b,
                                                        source_transform.c, source_transform.d,
                                                        source_transform.e, source_transform.f)]
    else:
        source_grid.pop("transform", None)
        source_grid["geolocation"] = "latitude_longitude_arrays"
    output = np.full((grid["height"], grid["width"]), np.nan, dtype=np.float32)
    valid = np.isfinite(source).astype(np.uint8)
    warped_valid = np.zeros(output.shape, dtype=np.uint8)
    source_georeferencing = ({"src_transform": source_transform} if source_transform is not None
                             else {"src_geoloc_array": (lon_mesh, lat_mesh)})
    source_crs = CRS.from_epsg(4326)
    reproject(source=source, destination=output, **source_georeferencing,
              src_crs=source_crs, src_nodata=np.nan, dst_transform=target_transform,
              dst_crs=target_crs, dst_nodata=np.nan, resampling=Resampling.bilinear,
              init_dest_nodata=True)
    reproject(source=valid, destination=warped_valid, **source_georeferencing,
              src_crs=source_crs, dst_transform=target_transform, dst_crs=target_crs,
              resampling=Resampling.nearest, init_dest_nodata=True)
    output[warped_valid == 0] = np.nan
    scale = _projection_scale(lat_mesh, lon_mesh, target_crs)
    source_name = str(source_passport["source"])
    source_version = source_passport.get("source_version")
    source_records = source_passport.get("sources")
    if isinstance(source_records, list) and source_records:
        source_records = [dict(item) for item in source_records if isinstance(item, dict)]
        if not source_records:
            raise ValueError("source passport must identify at least one safe source record")
    else:
        source_records = [{"provider": source_name,
                           "dataset_version": source_version or "not_recorded",
                           "source_algorithm_id": source_passport["algorithm_id"],
                           "source_algorithm_version": source_passport["algorithm_version"]}]
    source_limitations = source_passport.get("limitations", [])
    if not isinstance(source_limitations, list) or any(not isinstance(item, str) for item in source_limitations):
        source_limitations = []
    source_algorithm = source_passport.get("algorithm", {})
    if not isinstance(source_algorithm, dict):
        source_algorithm = {}
    source_algorithm_id = source_algorithm.get("id", source_passport.get("algorithm_id", "unknown"))
    source_algorithm_version = source_algorithm.get("version", source_passport.get("algorithm_version", "unknown"))
    source_grid_for_schema = dict(source_grid)
    source_grid_for_schema["coordinate_sha256"] = coord_digest
    common = product_passport(
        product_id="vorticity850-projected:{}:{}:{}".format(
            source_name, source_passport["algorithm_id"], source_passport["valid_time"]),
        algorithm_id="arktika.experimental_vorticity_reprojection",
        algorithm_version="0.1.0",
        maturity="experimental",
        quantity_kind="atmospheric_dynamics",
        quantity="relative_vorticity",
        units="s-1",
        valid_time=source_passport["valid_time"],
        data_kind=source_passport.get("data_kind", "analysis"),
        grid=grid,
        source_grids=[source_grid_for_schema],
        nodata="NaN; nearest source validity mask preserved",
        quality_control={"status": "technical_checks_applied",
                         "rules": ["source passport required", "explicit target CRS/transform/resolution",
                                   "nodata mask warped with nearest neighbour"]},
        sources=source_records,
        calibration_version="not_applicable",
        calibration_status="not_applicable",
        limitations=["Перепроецирование экспериментально; пригодность масштаба целевой проекции не валидирована.",
                     "Результат не является прогнозом или подтверждением ПМЦ.",
                     "Метеорологическая точность не проверялась."],
        uncertainty={"status": "not_assessed", "method": None},
    )
    passport = dict(source_passport)
    passport.update(common)
    passport["limitations"] = list(dict.fromkeys(
        [*source_limitations, *common.get("limitations", [])]))
    passport.update({"algorithm_id": "arktika.experimental_vorticity_reprojection",
                     "algorithm_version": "0.1.0", "source": source_name,
                     "source_version": source_version,
                     "variable": "relative_vorticity", "level_hPa": 850,
                     "source_algorithm_id": source_algorithm_id,
                     "source_algorithm_version": source_algorithm_version,
                     "source_grid": source_grid, "projection_scale": scale,
                     "input_coverage": {"valid_pixels": int(np.isfinite(output).sum()),
                                        "total_pixels": int(output.size),
                                        "valid_fraction": float(np.isfinite(output).mean())},
                     "reprojection": {"method": "rasterio.warp.reproject", "resampling": "bilinear",
                                      "source_georeferencing": ("affine_regular_grid" if source_transform is not None
                                                                 else "2d_latitude_longitude_geolocation_arrays"),
                                      "validity_mask_resampling": "nearest",
                                      "projection_scale_suitability": "not validated"}})
    return {"values": output, "grid": grid, "passport": passport}
