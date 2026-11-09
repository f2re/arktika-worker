"""Experimental, versioned polar-mesocyclone candidate detector.

This is a numerical research core, not an operational warning product. A
positive component is always labelled ``candidate``; satellite overlap is an
optional comparison and never upgrades a candidate to a confirmed PMC.

The vorticity thresholds and circular uniform-radius smoothing are based on
the low-threshold Nordic Seas IMPMCT settings (Fang et al., 2025). This
prototype defaults to retaining connected areas above 1.0e-4 s-1 whose peak
reaches 1.2e-4 s-1. It adds bilinearly interpolated marching-squares contour cores
plus nearest-projected-grid remainder, but saddle isolation is still a
discrete approximation and this is not the complete published
valley-line partition method. Temporal tracking/steering, satellite
morphology, and complete quality controls are absent. Its output is not a
reproduction or validation of IMPMCT.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from math import isfinite
from typing import Any

import numpy as np
from pyproj import CRS, Transformer
from scipy import ndimage
from rasterio.features import shapes
from rasterio.transform import Affine

from .product_schema import product_passport
from .pmc_partition import partition_component

ALGORITHM_ID = "arktika.experimental_pmc_candidates"
ALGORITHM_VERSION = "0.6.0"
METHOD_REFERENCE = "Fang et al. (2025), IMPMCT, doi:10.5194/essd-17-6049-2025"
MAX_CANDIDATE_GEOMETRY_VERTICES = 4096
PARAMETERS = {
    "uniform_radius_km": 60.0,
    "smoothing_operator": "circular uniform mean; nodata-renormalized; missing centre remains missing",
    "minimum_vorticity_s-1": 1.0e-4,
    "peak_vorticity_s-1": 1.2e-4,
    "peak_diagnostic": "8-neighbour local maxima with discrete max-tree saddles and experimental contour partition",
    "peak_isolation_gamma": 0.15,
    "peak_isolation_method": "discrete 8-neighbour max-tree; global peak uses component threshold reference",
    "peak_isolation_max_component_cells": 16384,
    "peak_isolation_max_local_maxima": 128,
    "equal_height_peak_policy": "fail_closed_component_diagnostic_unresolved",
    "partition_contour_interval_s-1": 1.0e-6,
    "partition_rule": "bilinear marching-squares core at first 1e-6 level above discrete max-tree saddle plus nearest projected-grid remainder; candidate-only",
    "connectivity": 8,
    "quantity": "850-hPa relative vorticity",
    "geographic_scope": "northern_hemisphere",
}


def validate_candidate_thresholds(
    overrides: Mapping[str, int | float] | None = None,
) -> dict[str, float]:
    """Resolve the two experimental vorticity thresholds in s⁻¹."""
    minimum_key = "minimum_vorticity_s-1"
    peak_key = "peak_vorticity_s-1"
    thresholds = {key: PARAMETERS[key] for key in (minimum_key, peak_key)}
    if overrides is None:
        return thresholds
    if not isinstance(overrides, Mapping) or set(overrides) - thresholds.keys():
        raise ValueError("Настройки ПМЦ принимают только пороги области и пика завихрённости.")
    for key, raw in overrides.items():
        try:
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError
            value = float(raw)
            if not isfinite(value) or value <= 0:
                raise ValueError
        except (ValueError, OverflowError):
            raise ValueError(f"Порог {key} должен быть конечным положительным числом в с⁻¹.") from None
        thresholds[key] = value
    if thresholds[peak_key] < thresholds[minimum_key]:
        raise ValueError("Порог пика завихрённости должен быть не ниже порога области.")
    return thresholds


def _validate_time(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("valid_time must be an ISO-8601 UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("valid_time must be an ISO-8601 UTC string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("valid_time must include UTC offset +00:00")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _uniform_radius_kernel(*, radius_km: float,
                           resolution_x_m: float, resolution_y_m: float) -> np.ndarray:
    """Return the uniform disk on the declared projected grid."""
    if any(not np.isfinite(value) or value <= 0
           for value in (radius_km, resolution_x_m, resolution_y_m)):
        raise ValueError("radius and grid resolutions must be finite positive values")
    radius_m = float(radius_km) * 1000.0
    rx = int(np.ceil(radius_m / float(resolution_x_m)))
    ry = int(np.ceil(radius_m / float(resolution_y_m)))
    x_m = np.arange(-rx, rx + 1, dtype=float) * float(resolution_x_m)
    y_m = np.arange(-ry, ry + 1, dtype=float) * float(resolution_y_m)
    return ((y_m[:, None] ** 2 + x_m[None, :] ** 2) <= radius_m ** 2 + 1e-9).astype(float)


def _smooth_uniform_radius(values: np.ndarray, *, radius_km: float,
                           resolution_x_m: float, resolution_y_m: float) -> np.ndarray:
    """Average valid neighbours without filling an unobserved centre cell."""
    array = np.ma.asarray(values, dtype=float).filled(np.nan)
    if array.ndim != 2:
        raise ValueError("vorticity smoothing requires a 2-D array")
    if np.isinf(array).any():
        raise ValueError("infinite vorticity values are invalid; use NaN for nodata")
    kernel = _uniform_radius_kernel(radius_km=radius_km,
                                    resolution_x_m=resolution_x_m,
                                    resolution_y_m=resolution_y_m)
    valid = np.isfinite(array)
    weights = ndimage.convolve(valid.astype(float), kernel, mode="constant", cval=0.0)
    totals = ndimage.convolve(np.where(valid, array, 0.0), kernel, mode="constant", cval=0.0)
    return np.divide(totals, weights, out=np.full_like(array, np.nan), where=valid & (weights > 0))


def _components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    seen = np.zeros(mask.shape, dtype=bool)
    found: list[list[tuple[int, int]]] = []
    height, width = mask.shape
    for row, col in zip(*np.nonzero(mask)):
        if seen[row, col]:
            continue
        seen[row, col] = True
        queue = deque([(int(row), int(col))])
        cells: list[tuple[int, int]] = []
        while queue:
            y, x = queue.popleft()
            cells.append((y, x))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if (0 <= ny < height and 0 <= nx < width and
                            not seen[ny, nx] and mask[ny, nx]):
                        seen[ny, nx] = True
                        queue.append((ny, nx))
        found.append(cells)
    return found


def _local_peak_diagnostics(field: np.ndarray, component: np.ndarray, *,
                            peak_threshold: float | None = None) -> tuple[list[dict[str, Any]], int]:
    """Describe local peaks without partitioning or changing a candidate area.

    This is a discrete diagnostic only. It does not reproduce the published
    contour/valley-line isolation procedure and must not be used as a vortex
    count or as evidence of a confirmed PMC.
    """
    if peak_threshold is None:
        peak_threshold = PARAMETERS["peak_vorticity_s-1"]
    neighborhood_max = ndimage.maximum_filter(
        np.where(np.isfinite(field), field, -np.inf), size=3, mode="constant", cval=-np.inf)
    peak_cells = component & np.isfinite(field) & (field >= peak_threshold)
    peak_cells &= field == neighborhood_max
    labels, total = ndimage.label(peak_cells, structure=np.ones((3, 3), dtype=np.uint8))
    if total == 0:
        return [], 0
    # Aggregate by raster label with fixed-size arrays; never materialize a Python
    # object per maximum. The preview is deterministic and bounded by the cap.
    flat_labels = labels.ravel()
    occupied = np.flatnonzero(flat_labels)
    ids = flat_labels[occupied]
    rows, cols = np.unravel_index(occupied, labels.shape)
    values = field.ravel()[occupied]
    counts = np.bincount(ids, minlength=total + 1)
    row_sums = np.bincount(ids, weights=rows, minlength=total + 1)
    col_sums = np.bincount(ids, weights=cols, minlength=total + 1)
    maxima = np.full(total + 1, -np.inf)
    np.maximum.at(maxima, ids, values)
    first_positions = np.full(total + 1, flat_labels.size, dtype=np.int64)
    np.minimum.at(first_positions, ids, occupied)
    peak_ids = np.arange(1, total + 1)
    order = np.lexsort((col_sums[peak_ids] / counts[peak_ids],
                        row_sums[peak_ids] / counts[peak_ids], -maxima[peak_ids]))
    cap = int(PARAMETERS["peak_isolation_max_local_maxima"])
    preview = []
    for label in peak_ids[order[:cap]]:
        preview.append({
            "peak_vorticity_s-1": float(maxima[label]),
            "peak_pixel_rc": [float(row_sums[label] / counts[label]),
                              float(col_sums[label] / counts[label])],
            "_seed_rc": [int(first_positions[label] // labels.shape[1]),
                         int(first_positions[label] % labels.shape[1])],
            "plateau_pixel_count": int(counts[label]),
        })
    return preview, int(total)


def _peak_saddle_diagnostics(field: np.ndarray, component: np.ndarray,
                             peaks: list[dict[str, Any]],
                             threshold: float) -> list[dict[str, Any]] | None:
    """Estimate peak isolation by maximin connectivity on the discrete grid.

    A descending-value union-find sweep records the merge level of each peak
    into a stronger peak. This discrete 8-neighbour max-tree approximation is
    not contour interpolation or the published valley-line method.
    """
    if not peaks:
        return []
    rows, cols = np.nonzero(component)
    if (len(rows) > PARAMETERS["peak_isolation_max_component_cells"]
            or len(peaks) > PARAMETERS["peak_isolation_max_local_maxima"]):
        return None
    peak_values = np.asarray([float(peak["peak_vorticity_s-1"]) for peak in peaks])
    if np.unique(peak_values).size != len(peak_values):
        # Equal-height maxima form an unresolved competition group for this
        # discrete max-tree diagnostic. Do not let scan order select an
        # arbitrary elder peak and assign unequal isolation labels.
        return [{
            "peak_vorticity_s-1": float(peak["peak_vorticity_s-1"]),
            "peak_pixel_rc": peak["peak_pixel_rc"],
            "peak_seed_pixel_rc": peak["_seed_rc"],
            "saddle_vorticity_s-1": None,
            "relative_isolation_contrast": None,
            "isolated_by_experimental_gamma": None,
            "isolation_status": "unresolved_equal_height_peak_tie",
            "plateau_pixel_count": peak["plateau_pixel_count"],
        } for peak in peaks]
    y0, y1 = int(rows.min()), int(rows.max()) + 1
    x0, x1 = int(cols.min()), int(cols.max()) + 1
    local_component = component[y0:y1, x0:x1]
    local_field = field[y0:y1, x0:x1]
    flat_component = local_component.ravel()
    flat_values = local_field.ravel()
    cells = np.flatnonzero(flat_component)
    order = cells[np.argsort(-flat_values[cells], kind="stable")]
    parent = np.full(flat_component.size, -1, dtype=np.int32)
    labels = np.full(flat_component.size, -1, dtype=np.int32)
    peak_by_seed = {((peak["_seed_rc"][0] - y0) * local_field.shape[1]
                     + peak["_seed_rc"][1] - x0): index
                    for index, peak in enumerate(peaks)}
    saddles: list[float | None] = [None] * len(peaks)

    def find(node: int) -> int:
        root = node
        while parent[root] != root:
            root = int(parent[root])
        while node != root:
            following = int(parent[node])
            parent[node] = root
            node = following
        return root

    width = local_field.shape[1]
    for cell in order:
        cell = int(cell)
        y, x = divmod(cell, width)
        parent[cell] = cell
        labels[cell] = peak_by_seed.get(cell, -1)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                ny, nx = y + dy, x + dx
                if not (0 <= ny < local_field.shape[0] and 0 <= nx < width):
                    continue
                neighbor = ny * width + nx
                if parent[neighbor] < 0:
                    continue
                left, right = find(cell), find(neighbor)
                if left == right:
                    continue
                left_peak, right_peak = int(labels[left]), int(labels[right])
                if left_peak >= 0 and right_peak >= 0:
                    if peaks[left_peak]["peak_vorticity_s-1"] >= peaks[right_peak]["peak_vorticity_s-1"]:
                        stronger, weaker = left_peak, right_peak
                    else:
                        stronger, weaker = right_peak, left_peak
                    if saddles[weaker] is None:
                        saddles[weaker] = float(local_field[y, x])
                    labels[left] = stronger
                elif right_peak >= 0:
                    labels[left] = right_peak
                parent[right] = left

    diagnostics = []
    for index, peak in enumerate(peaks):
        saddle = float(threshold) if index == 0 or saddles[index] is None else saddles[index]
        peak_value = float(peak["peak_vorticity_s-1"])
        contrast = (peak_value - saddle) / peak_value if peak_value > 0 else 0.0
        diagnostics.append({
            "peak_vorticity_s-1": peak_value,
            "peak_pixel_rc": peak["peak_pixel_rc"],
            "peak_seed_pixel_rc": peak["_seed_rc"],
            "saddle_vorticity_s-1": saddle,
            "relative_isolation_contrast": contrast,
            "isolated_by_experimental_gamma": contrast > PARAMETERS["peak_isolation_gamma"],
            "plateau_pixel_count": peak["plateau_pixel_count"],
        })
    return diagnostics


def _satellite_times(metadata: dict[str, Any], candidate_valid_time: str | None) -> dict[str, str]:
    """Normalize the nominal UTC instant and the optional closed scan interval."""
    nominal = _validate_time(metadata.get("valid_time"))
    if candidate_valid_time is not None:
        candidate_valid_time = _validate_time(candidate_valid_time)
    start = metadata.get("scan_start_utc")
    end = metadata.get("scan_end_utc")
    if start is None and end is None:
        if candidate_valid_time is not None and nominal != candidate_valid_time:
            raise ValueError("Срок спутниковой маски без интервала съёмки должен совпадать со сроком реанализа.")
        return {"valid_time": nominal}
    if start is None or end is None:
        raise ValueError("Интервал съёмки требует одновременно scan_start_utc и scan_end_utc.")

    start = _validate_time(start)
    end = _validate_time(end)
    start_stamp = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_stamp = datetime.fromisoformat(end.replace("Z", "+00:00"))
    nominal_stamp = datetime.fromisoformat(nominal.replace("Z", "+00:00"))
    if start_stamp >= end_stamp or not start_stamp <= nominal_stamp <= end_stamp:
        raise ValueError("Интервал съёмки должен иметь положительную длительность и содержать срок маски.")
    if candidate_valid_time is not None:
        model_stamp = datetime.fromisoformat(candidate_valid_time.replace("Z", "+00:00"))
        if not start_stamp <= model_stamp <= end_stamp:
            raise ValueError("Срок реанализа должен попадать в интервал съёмки.")
    return {"valid_time": nominal, "scan_start_utc": start, "scan_end_utc": end}


def compare_satellite(candidate_mask: np.ndarray,
                      satellite_observation_mask: np.ndarray | None,
                      valid_mask: np.ndarray | None = None,
                      observation_metadata: dict[str, Any] | None = None,
                      candidate_grid: dict[str, Any] | None = None,
                      coverage_mask: np.ndarray | None = None, *,
                      candidate_valid_time: str | None = None) -> dict[str, Any]:
    """Compare candidates only where the satellite supplied a valid observation.

    A missing coverage mask is rejected: a zero in a label mask means an
    observed negative only inside coverage, while every pixel outside coverage
    remains unknown and is excluded from contingency metrics.
    """
    if satellite_observation_mask is None:
        return {"status": "not_supplied", "metrics": None}
    required_metadata = ("source", "product_id", "valid_time", "observation_id", "grid")
    if not isinstance(observation_metadata, dict) or any(
            key != "grid" and (not isinstance(observation_metadata.get(key), str) or
            not observation_metadata[key].strip()) for key in required_metadata):
        raise ValueError("satellite observations require source, product_id, valid_time, observation_id and grid provenance")
    if coverage_mask is None:
        raise ValueError("satellite comparison requires an explicit coverage mask")
    candidates = np.asarray(candidate_mask, dtype=bool)
    valid = np.ones(candidates.shape, dtype=bool) if valid_mask is None else np.asarray(valid_mask, dtype=bool)
    if valid.shape != candidates.shape:
        raise ValueError("comparison valid mask must match candidate grid")
    observed = np.asarray(satellite_observation_mask)
    if observed.shape != candidates.shape or observed.dtype.kind not in "biu":
        raise ValueError("satellite observation mask must be a same-grid boolean/integer array")
    coverage = np.asarray(coverage_mask)
    if coverage.shape != candidates.shape or coverage.dtype.kind not in "biu":
        raise ValueError("satellite coverage mask must be a same-grid boolean/integer array")
    if not np.isin(observed, (0, 1)).all() or not np.isin(coverage, (0, 1)).all():
        raise ValueError("satellite label and coverage masks must contain only 0/1 values")
    if not _same_grid(candidate_grid, observation_metadata.get("grid")):
        raise ValueError("satellite observation grid must exactly match candidate grid georeferencing")
    observed = observed.astype(bool)
    coverage = coverage.astype(bool)
    observation = {key: (dict(observation_metadata[key]) if key == "grid"
                         else observation_metadata[key].strip())
                   for key in required_metadata}
    observation.update(_satellite_times(observation_metadata, candidate_valid_time))
    regridding = observation_metadata.get("regridding")
    if isinstance(regridding, dict):
        method = regridding.get("method")
        source_grid = regridding.get("source_grid")
        target_grid = regridding.get("target_grid")
        if method in {"identity", "nearest_neighbor"}:
            try:
                from .pmc_archive_run import validate_grid
                from .pmc_projection import (
                    MAX_OBSERVATION_SOURCE_PIXELS,
                    _validate_observation_source_grid,
                )
                source_grid = _validate_observation_source_grid(
                    source_grid, MAX_OBSERVATION_SOURCE_PIXELS)
                target_grid = validate_grid(target_grid)
            except (TypeError, ValueError):
                pass
            else:
                if _same_grid(candidate_grid, target_grid):
                    observation["regridding"] = {
                        "method": method,
                        "source_grid": source_grid,
                        "target_grid": target_grid,
                    }
    comparison_domain = valid & coverage
    observed_pixels = int(np.count_nonzero(comparison_domain))
    if not observed_pixels:
        return {
            "status": "matched_no_coverage",
            "observation": observation,
            "metrics": None,
            "coverage": {
                "valid_model_pixels": int(np.count_nonzero(valid)),
                "observed_pixels": 0,
                "unknown_pixels": int(np.count_nonzero(valid)),
                "candidate_pixels_outside_coverage": int(np.count_nonzero(valid & candidates)),
            },
        }
    tp = int(np.count_nonzero(comparison_domain & candidates & observed))
    fp = int(np.count_nonzero(comparison_domain & candidates & ~observed))
    fn = int(np.count_nonzero(comparison_domain & ~candidates & observed))
    tn = int(np.count_nonzero(comparison_domain & ~candidates & ~observed))
    union = tp + fp + fn
    return {
        "status": "comparison_only",
        "observation": observation,
        "metrics": {
            "true_positive_pixels": tp,
            "false_positive_pixels": fp,
            "false_negative_pixels": fn,
            "true_negative_pixels": tn,
            "intersection_over_union": tp / union if union else None,
            "probability_of_detection": tp / (tp + fn) if tp + fn else None,
            "false_alarm_ratio": fp / (tp + fp) if tp + fp else None,
        },
        "coverage": {
            "valid_model_pixels": int(np.count_nonzero(valid)),
            "observed_pixels": observed_pixels,
            "unknown_pixels": int(np.count_nonzero(valid & ~coverage)),
            "candidate_pixels_outside_coverage": int(np.count_nonzero(valid & candidates & ~coverage)),
        },
        "interpretation": "Пространственное сравнение масок; не подтверждает физическую классификацию ПМЦ.",
    }


def _same_grid(first: dict[str, Any] | None, second: dict[str, Any] | None) -> bool:
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    keys = ("crs", "width", "height", "resolution_x_m", "resolution_y_m", "transform")
    return all(first.get(key) == second.get(key) for key in keys)


def _candidate_geometry(mask: np.ndarray) -> dict[str, Any]:
    """Export cell edges of the detected threshold region, without smoothing.

    Rings may include holes and disconnected parts. Consumers must use an
    even-odd fill rule. These are pixel edges, not sample centres or a cloud
    outline; the declared affine transform is applied without a 0.5 offset.
    """
    base = {"coordinate_system": "pixel_edges_rc",
            "kind": "threshold_region_pixel_outline",
            "fill_rule": "evenodd", "vertex_limit": MAX_CANDIDATE_GEOMETRY_VERTICES}
    rows, cols = np.nonzero(mask)
    if not len(rows):
        return {**base, "status": "complete", "rings": [], "vertex_count": 0}
    top, left = int(rows.min()), int(cols.min())
    bottom, right = int(rows.max()) + 1, int(cols.max()) + 1
    local = np.asarray(mask[top:bottom, left:right], dtype=np.uint8)
    rings: list[list[list[float]]] = []
    vertex_count = 0
    for geometry, _ in shapes(local, mask=local.astype(bool), connectivity=4,
                              transform=Affine.translation(left, top)):
        for ring in geometry["coordinates"]:
            vertex_count += len(ring)
            if vertex_count > MAX_CANDIDATE_GEOMETRY_VERTICES:
                return {**base, "status": "omitted_complexity_limit", "rings": [],
                        "vertex_count": None}
            rings.append([[float(row), float(col)] for col, row in ring])
    return {**base, "status": "complete", "rings": rings, "vertex_count": vertex_count}


def detect_candidates(vorticity_850: np.ndarray, *, grid: dict[str, Any],
                      valid_time: str, source: str,
                      source_field_metadata: dict[str, Any],
                      satellite_observation_mask: np.ndarray | None = None,
                      satellite_observation_metadata: dict[str, Any] | None = None,
                      satellite_coverage_mask: np.ndarray | None = None,
                      domain_mask: np.ndarray | None = None,
                      thresholds: Mapping[str, int | float] | None = None) -> dict[str, Any]:
    """Detect experimental circulation candidates from 850-hPa relative vorticity.

    Required grid keys include projected metric ``crs``, dimensions, cell sizes,
    and a six-value affine ``transform``. The input field passport must declare
    relative vorticity at 850 hPa in s-1 and the matching source/time/grid. ``source``
    must identify provenance (for example ERA5/CARRA/MERRA-2 plus dataset id).
    Threshold overrides are in s⁻¹ and do not alter the smoothing radius.
    """
    applied_thresholds = validate_candidate_thresholds(thresholds)
    array = np.ma.asarray(vorticity_850, dtype=float).filled(np.nan)
    if array.ndim != 2 or min(array.shape, default=0) < 1:
        raise ValueError("850-hPa vorticity must be a non-empty 2-D array")
    if np.isinf(array).any():
        raise ValueError("infinite vorticity values are invalid; use NaN for nodata")
    if not isinstance(grid, dict):
        raise ValueError("grid metadata is required")
    required = ("crs", "width", "height", "resolution_x_m", "resolution_y_m", "transform")
    if any(key not in grid for key in required):
        raise ValueError("grid requires CRS, dimensions, and metric cell resolutions")
    if (grid["width"], grid["height"]) != (array.shape[1], array.shape[0]):
        raise ValueError("grid dimensions do not match input array")
    try:
        crs = CRS.from_user_input(grid["crs"])
    except Exception as exc:
        raise ValueError("a valid projected metric CRS is required") from exc
    if not crs.is_projected or not crs.axis_info or any(
            abs(float(axis.unit_conversion_factor or 0) - 1.0) > 1e-9 for axis in crs.axis_info[:2]):
        raise ValueError("a projected metric CRS with metre axes is required")
    try:
        dx, dy = float(grid["resolution_x_m"]), float(grid["resolution_y_m"])
    except (TypeError, ValueError) as exc:
        raise ValueError("grid resolutions must be positive metres") from exc
    if not np.isfinite(dx) or not np.isfinite(dy) or dx <= 0 or dy <= 0:
        raise ValueError("grid resolutions must be positive metres")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source provenance is required")
    timestamp = _validate_time(valid_time)
    if not isinstance(grid["transform"], (list, tuple)) or len(grid["transform"]) != 6:
        raise ValueError("grid requires the full six-value affine transform")
    try:
        transform = [float(value) for value in grid["transform"]]
    except (TypeError, ValueError) as exc:
        raise ValueError("grid transform must contain finite numbers") from exc
    if not np.isfinite(transform).all():
        raise ValueError("grid transform must contain finite numbers")
    if (transform[0] == 0 or transform[4] == 0
            or abs(transform[1]) > 1e-12 or abs(transform[3]) > 1e-12
            or not np.isclose(abs(transform[0]), dx, rtol=1e-9, atol=1e-9)
            or not np.isclose(abs(transform[4]), dy, rtol=1e-9, atol=1e-9)):
        raise ValueError("grid transform must be unrotated, nonsingular and match metric resolutions")
    if (not isinstance(source_field_metadata, dict)
            or source_field_metadata.get("variable") != "relative_vorticity"
            or source_field_metadata.get("level_hPa") != 850
            or source_field_metadata.get("units") != "s-1"
            or source_field_metadata.get("source") != source.strip()
            or _validate_time(source_field_metadata.get("valid_time")) != timestamp
            or not _same_grid(grid, source_field_metadata.get("grid"))):
        raise ValueError("source field passport must match source, 850-hPa relative vorticity, s-1, UTC time, and exact grid")
    domain = np.ones(array.shape, dtype=bool)
    if domain_mask is not None:
        raw_domain = np.ma.asarray(domain_mask)
        if (raw_domain.shape != array.shape or raw_domain.dtype.kind not in "biu"
                or np.ma.getmaskarray(raw_domain).any()
                or not np.isin(raw_domain, (0, 1)).all() or not raw_domain.any()):
            raise ValueError("domain mask must match the vorticity grid, contain only 0/1 and include at least one cell")
        domain = np.asarray(raw_domain, dtype=bool)
    # The published Nordic-Seas thresholds apply to positive (cyclonic)
    # vorticity in the Northern Hemisphere. Fail closed for southern/equatorial
    # domains rather than silently detecting southern anticyclones.
    rows, cols = np.nonzero(domain)
    geographic = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    _, latitudes = geographic.transform(
        transform[0] * (cols + 0.5) + transform[2],
        transform[4] * (rows + 0.5) + transform[5])
    if not np.isfinite(latitudes).all() or np.any(np.asarray(latitudes) <= 0):
        raise ValueError("Northern Hemisphere domain required: positive vorticity is not cyclonic south of the equator")
    smoothed = _smooth_uniform_radius(
        array, radius_km=PARAMETERS["uniform_radius_km"],
        resolution_x_m=dx, resolution_y_m=dy)
    # Keep incomplete 60-km neighbourhoods visible in the quality record.
    # Renormalization is useful near a missing edge, but changes the effective
    # filter; it must not be mistaken for the complete published disk mean.
    complete_support = ndimage.binary_erosion(
        np.isfinite(array), structure=_uniform_radius_kernel(
            radius_km=PARAMETERS["uniform_radius_km"],
            resolution_x_m=dx, resolution_y_m=dy).astype(bool), border_value=0)
    threshold = applied_thresholds["minimum_vorticity_s-1"]
    peak_threshold = applied_thresholds["peak_vorticity_s-1"]
    candidate_mask = np.zeros(array.shape, dtype=bool)
    candidates: list[dict[str, Any]] = []
    for cells in _components(np.isfinite(smoothed) & (smoothed >= threshold) & domain):
        ys = np.fromiter((cell[0] for cell in cells), dtype=int)
        xs = np.fromiter((cell[1] for cell in cells), dtype=int)
        peak_index = int(np.nanargmax(smoothed[ys, xs]))
        peak = float(smoothed[ys[peak_index], xs[peak_index]])
        if peak < peak_threshold:
            continue
        component = np.zeros(array.shape, dtype=bool)
        component[ys, xs] = True
        expanded = ndimage.binary_dilation(component, structure=np.ones((3, 3), dtype=bool))
        invalid = ~np.isfinite(smoothed) | ~domain
        boundary_censored = bool(
            component[0, :].any() or component[-1, :].any()
            or component[:, 0].any() or component[:, -1].any()
            or np.any(expanded & invalid))
        component_too_large = len(cells) > PARAMETERS["peak_isolation_max_component_cells"]
        if component_too_large:
            local_peaks, local_peak_count = [], None
        else:
            local_peaks, local_peak_count = _local_peak_diagnostics(
                smoothed, component, peak_threshold=peak_threshold)
        over_peak_limit = (local_peak_count is not None
                           and local_peak_count > PARAMETERS["peak_isolation_max_local_maxima"])
        saddle_peaks = None if (component_too_large or over_peak_limit) else _peak_saddle_diagnostics(
            smoothed, component, local_peaks, threshold)
        unresolved_peak_tie = bool(saddle_peaks and any(
            item.get("isolation_status") == "unresolved_equal_height_peak_tie"
            for item in saddle_peaks))
        candidate_mask[ys, xs] = True
        if saddle_peaks is None or unresolved_peak_tie:
            if saddle_peaks is None:
                saddle_peaks = [{
                    "peak_vorticity_s-1": local_peak["peak_vorticity_s-1"],
                    "peak_pixel_rc": local_peak["peak_pixel_rc"],
                    "peak_seed_pixel_rc": local_peak["_seed_rc"],
                    "saddle_vorticity_s-1": None,
                    "relative_isolation_contrast": None,
                    "isolated_by_experimental_gamma": None,
                    "isolation_status": "not_computed_component_complexity_limit",
                    "plateau_pixel_count": local_peak["plateau_pixel_count"],
                } for local_peak in local_peaks]
            status = ("unresolved_equal_height_peak_tie_unpartitioned" if unresolved_peak_tie
                      else "component_complexity_limit_unpartitioned")
            partition = {"status": status,
                         "labels": component.astype(np.int16),
                         "regions": [{"label": 1, "pixel_count": int(len(cells)),
                                      "centroid_pixel_rc": [float(np.mean(ys)), float(np.mean(xs))],
                                      "classification": "candidate",
                                      "confirmation_status": "not_confirmed"}],
                         "coverage_conserved": True}
        else:
            partition = partition_component(
                smoothed, component, saddle_peaks, threshold=threshold,
                grid=grid, boundary_censored=boundary_censored)
        if not partition["coverage_conserved"]:
            raise RuntimeError("experimental PMC partition did not conserve candidate pixels")
        peak_structure = {
            "status": partition["status"],
            "method": "bilinear_marching_squares_cores_above_discrete_max_tree_saddles",
            "contour_interval_s-1": PARAMETERS["partition_contour_interval_s-1"],
            "gamma": PARAMETERS["peak_isolation_gamma"],
            "local_maximum_count": local_peak_count,
            "local_maxima_preview_truncated": over_peak_limit,
            "local_maxima": [{key: value for key, value in item.items() if not key.startswith("_")}
                             for item in saddle_peaks],
            "coverage_conserved": partition["coverage_conserved"],
            "regions": partition["regions"],
        }
        for region in partition["regions"]:
            region_mask = partition["labels"] == region["label"]
            incomplete_cells = int(np.count_nonzero(region_mask & ~complete_support))
            projected_area_km2 = float(region["pixel_count"] * dx * dy / 1e6)
            candidates.append({
                "candidate_id": f"pmc-candidate-{len(candidates) + 1:04d}",
                "classification": "candidate",
                "confirmation_status": "not_confirmed",
                "pixel_count": region["pixel_count"],
                "peak_850hpa_relative_vorticity_s-1": region.get(
                    "peak_vorticity_s-1", peak),
                "peak_pixel_rc": region.get("peak_pixel_rc", [int(ys[peak_index]), int(xs[peak_index])]),
                "centroid_pixel_rc": region["centroid_pixel_rc"],
                "partition_status": partition["status"],
                "projected_area_km2": projected_area_km2,
                "projected_equivalent_diameter_km": float(2 * np.sqrt(projected_area_km2 / np.pi)),
                "geometry": _candidate_geometry(region_mask),
                "spatial_quality": {
                    "boundary_censored": boundary_censored,
                    "complete_smoothing_support": incomplete_cells == 0,
                    "incomplete_smoothing_cell_count": incomplete_cells,
                    "area_basis": "projected_grid; not geodesic area or a physical cyclone-size criterion",
                },
                "peak_structure": peak_structure,
            })
    limitations = [
        "Кандидаты не являются подтверждёнными ПМЦ или прогнозом зарождения.",
        "Границы ядер интерполируются билинейным marching-squares, но уровни седла остаются дискретной max-tree оценкой, а остаток назначается ближайшему seed в проецированной сетке; это не полное contour/valley-line разделение IMPMCT и требует анализа чувствительности и метеорологической проверки.",
        "Равные по высоте пики в одной связной области оставляют диагностическое разделение неразрешённым; это не означает одинаковую физическую структуру вихрей.",
        "Архивный трекер использует соседство центроидов; steering flow, split/merge и опубликованные QC-процедуры отсутствуют.",
        "Спутниковое сравнение описывает только пиксельное перекрытие заданных масок и не является независимой метеорологической верификацией.",
        "Искажение масштаба конкретной проекции не оценивается; пригодность сетки для заданной широты требует отдельной проверки.",
        "Положительная завихрённость применяется только в Северном полушарии; параметры из Норвежского, Гренландского и Баренцева морей не валидированы для всех арктических районов.",
        "Маскированные и отсутствующие узлы остаются неизвестными после сглаживания; неполные окрестности 60 км отмечаются отдельно, их среднее не эквивалентно полному круговому фильтру.",
        "Морская среда, суша и рельеф, термодинамическая неустойчивость, приземный ветер и облачная морфология не проверяются этим детектором.",
    ]
    valid_pixels = int(np.count_nonzero(np.isfinite(smoothed) & domain))
    source_record = {"source_id": source.strip()}
    for key in ("product_id", "field_id", "input_sha256"):
        value = source_field_metadata.get(key)
        if isinstance(value, str) and value.strip():
            source_record[key] = value.strip()
    passport = product_passport(
        product_id="arktika.pmc.candidates",
        algorithm_id=ALGORITHM_ID,
        algorithm_version=ALGORITHM_VERSION,
        maturity="experimental",
        quantity_kind="categorical_classification",
        quantity="Кандидаты циркуляции, не подтверждённые ПМЦ",
        units="1",
        valid_time=timestamp,
        data_kind="analysis",
        grid={key: grid[key] for key in required},
        source_grids=[dict(source_field_metadata.get("grid", grid))],
        nodata="NaN входной вихревости исключён из обработки; отсутствующее наблюдение не считается отрицательным классом",
        quality_control={
            "status": "experimental_screening",
            "valid_cells": valid_pixels,
            "missing_or_outside_domain_cells": int(array.size - valid_pixels),
            "candidate_count": len(candidates),
            "geographic_scope": "northern_hemisphere",
            "incomplete_smoothing_cells": int(np.count_nonzero(np.isfinite(smoothed) & domain & ~complete_support)),
            "physical_classification": "not_evaluated; vorticity_candidates_only",
            "thresholds": applied_thresholds,
            "satellite_comparison_status": "not_supplied" if satellite_observation_mask is None else "provided",
        },
        sources=[source_record],
        calibration_version="not_applicable",
        calibration_status="not_applicable",
        limitations=limitations,
        uncertainty={"status": "not_assessed", "method": None},
    )
    return {
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "maturity": "experimental",
        "method_reference": METHOD_REFERENCE,
        "valid_time": timestamp,
        "source": source.strip(),
        "input": {"quantity": "850-hPa relative vorticity", "units": "s-1",
                  "shape": list(array.shape), "time": timestamp,
                  "source_field_metadata_required": ["isobaric_level_hPa", "quantity", "units", "time"]},
        "grid": {key: grid[key] for key in required},
        "passport": passport,
        "parameters": {**PARAMETERS, **applied_thresholds},
        "candidate_count": len(candidates),
        "candidates": candidates,
        "satellite_comparison": compare_satellite(
            candidate_mask, satellite_observation_mask, np.isfinite(smoothed) & domain,
            satellite_observation_metadata, grid, satellite_coverage_mask,
            candidate_valid_time=timestamp,
        ),
        "limitations": limitations,
    }
