"""Experimental, versioned polar-mesocyclone candidate detector.

This is a numerical research core, not an operational warning product. A
positive component is always labelled ``candidate``; satellite overlap is an
optional comparison and never upgrades a candidate to a confirmed PMC.

The vorticity thresholds and circular uniform-radius smoothing are based on
the low-threshold Nordic Seas IMPMCT settings (Fang et al., 2025). This
prototype retains connected areas above 1.0e-4 s-1 whose peak reaches
1.2e-4 s-1. It does not implement the paper's peak partitioning, temporal
tracking/steering, satellite morphology model, or complete quality controls.
Consequently its output is not a reproduction or validation of IMPMCT.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

import numpy as np
from pyproj import CRS
from scipy import ndimage

ALGORITHM_ID = "arktika.experimental_pmc_candidates"
ALGORITHM_VERSION = "0.2.0"
METHOD_REFERENCE = "Fang et al. (2025), IMPMCT, doi:10.5194/essd-17-6049-2025"
PARAMETERS = {
    "uniform_radius_km": 60.0,
    "smoothing_operator": "circular uniform mean; nodata-renormalized",
    "minimum_vorticity_s-1": 1.0e-4,
    "peak_vorticity_s-1": 1.2e-4,
    "connectivity": 8,
    "quantity": "850-hPa relative vorticity",
}


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


def _smooth_uniform_radius(values: np.ndarray, *, radius_km: float,
                           resolution_x_m: float, resolution_y_m: float) -> np.ndarray:
    """Apply a circular uniform mean in metres, renormalized over valid cells."""
    array = np.asarray(values, dtype=float)
    if array.ndim != 2:
        raise ValueError("vorticity smoothing requires a 2-D array")
    if any(not np.isfinite(value) or value <= 0
           for value in (radius_km, resolution_x_m, resolution_y_m)):
        raise ValueError("radius and grid resolutions must be finite positive values")
    radius_m = float(radius_km) * 1000.0
    rx = int(np.ceil(radius_m / float(resolution_x_m)))
    ry = int(np.ceil(radius_m / float(resolution_y_m)))
    x_m = np.arange(-rx, rx + 1, dtype=float) * float(resolution_x_m)
    y_m = np.arange(-ry, ry + 1, dtype=float) * float(resolution_y_m)
    footprint = (y_m[:, None] ** 2 + x_m[None, :] ** 2) <= radius_m ** 2 + 1e-9
    kernel = footprint.astype(float)
    valid = np.isfinite(array)
    weights = ndimage.convolve(valid.astype(float), kernel, mode="constant", cval=0.0)
    totals = ndimage.convolve(np.where(valid, array, 0.0), kernel, mode="constant", cval=0.0)
    return np.divide(totals, weights, out=np.full_like(array, np.nan), where=weights > 0)


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


def compare_satellite(candidate_mask: np.ndarray,
                      satellite_observation_mask: np.ndarray | None,
                      valid_mask: np.ndarray | None = None,
                      observation_metadata: dict[str, Any] | None = None,
                      candidate_grid: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return spatial contingency metrics only for a supplied observed mask."""
    if satellite_observation_mask is None:
        return {"status": "not_supplied", "metrics": None}
    required_metadata = ("source", "product_id", "valid_time", "observation_id", "grid")
    if not isinstance(observation_metadata, dict) or any(
            key != "grid" and (not isinstance(observation_metadata.get(key), str) or
            not observation_metadata[key].strip()) for key in required_metadata):
        raise ValueError("satellite observations require source, product_id, valid_time, observation_id and grid provenance")
    candidates = np.asarray(candidate_mask, dtype=bool)
    valid = np.ones(candidates.shape, dtype=bool) if valid_mask is None else np.asarray(valid_mask, dtype=bool)
    if valid.shape != candidates.shape:
        raise ValueError("comparison valid mask must match candidate grid")
    observed = np.asarray(satellite_observation_mask)
    if observed.shape != candidates.shape or observed.dtype.kind not in "biu":
        raise ValueError("satellite observation mask must be a same-grid boolean/integer array")
    if not _same_grid(candidate_grid, observation_metadata.get("grid")):
        raise ValueError("satellite observation grid must exactly match candidate grid georeferencing")
    observed = observed.astype(bool)
    tp = int(np.count_nonzero(valid & candidates & observed))
    fp = int(np.count_nonzero(valid & candidates & ~observed))
    fn = int(np.count_nonzero(valid & ~candidates & observed))
    tn = int(np.count_nonzero(valid & ~candidates & ~observed))
    union = tp + fp + fn
    return {
        "status": "comparison_only",
        "observation": {key: (dict(observation_metadata[key]) if key == "grid"
                              else observation_metadata[key].strip())
                        for key in required_metadata},
        "metrics": {
            "true_positive_pixels": tp,
            "false_positive_pixels": fp,
            "false_negative_pixels": fn,
            "true_negative_pixels": tn,
            "intersection_over_union": tp / union if union else None,
            "probability_of_detection": tp / (tp + fn) if tp + fn else None,
            "false_alarm_ratio": fp / (tp + fp) if tp + fp else None,
        },
        "interpretation": "Пространственное сравнение масок; не подтверждает физическую классификацию ПМЦ.",
    }


def _same_grid(first: dict[str, Any] | None, second: dict[str, Any] | None) -> bool:
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    keys = ("crs", "width", "height", "resolution_x_m", "resolution_y_m", "transform")
    return all(first.get(key) == second.get(key) for key in keys)


def detect_candidates(vorticity_850: np.ndarray, *, grid: dict[str, Any],
                      valid_time: str, source: str,
                      source_field_metadata: dict[str, Any],
                      satellite_observation_mask: np.ndarray | None = None,
                      satellite_observation_metadata: dict[str, Any] | None = None,
                      domain_mask: np.ndarray | None = None) -> dict[str, Any]:
    """Detect experimental circulation candidates from 850-hPa relative vorticity.

    Required grid keys include projected metric ``crs``, dimensions, cell sizes,
    and a six-value affine ``transform``. The input field passport must declare
    relative vorticity at 850 hPa in s-1 and the matching source/time/grid. ``source``
    must identify provenance (for example ERA5/CARRA/MERRA-2 plus dataset id).
    """
    array = np.asarray(vorticity_850, dtype=float)
    if array.ndim != 2 or min(array.shape, default=0) < 1:
        raise ValueError("850-hPa vorticity must be a non-empty 2-D array")
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
    if (not isinstance(source_field_metadata, dict)
            or source_field_metadata.get("variable") != "relative_vorticity"
            or source_field_metadata.get("level_hPa") != 850
            or source_field_metadata.get("units") != "s-1"
            or source_field_metadata.get("source") != source.strip()
            or _validate_time(source_field_metadata.get("valid_time")) != timestamp
            or not _same_grid(grid, source_field_metadata.get("grid"))):
        raise ValueError("source field passport must match source, 850-hPa relative vorticity, s-1, UTC time, and exact grid")
    if satellite_observation_mask is not None:
        satellite_metadata = satellite_observation_metadata or {}
        if any(key not in satellite_metadata for key in ("source", "product_id", "valid_time", "observation_id", "grid")):
            raise ValueError("satellite observations require source, product_id, valid_time, observation_id and grid provenance")
        if _validate_time(satellite_metadata.get("valid_time")) != timestamp:
            raise ValueError("satellite observation time must match the reanalysis valid time")
        observed_grid = satellite_metadata.get("grid")
        if not _same_grid(grid, observed_grid):
            raise ValueError("satellite observation grid metadata must exactly match the reanalysis grid")

    smoothed = _smooth_uniform_radius(
        array, radius_km=PARAMETERS["uniform_radius_km"],
        resolution_x_m=dx, resolution_y_m=dy)
    domain = np.ones(array.shape, dtype=bool)
    if domain_mask is not None:
        domain = np.asarray(domain_mask, dtype=bool)
        if domain.shape != array.shape or not domain.any():
            raise ValueError("domain mask must match the vorticity grid and include at least one cell")
    threshold = PARAMETERS["minimum_vorticity_s-1"]
    peak_threshold = PARAMETERS["peak_vorticity_s-1"]
    candidate_mask = np.zeros(array.shape, dtype=bool)
    candidates: list[dict[str, Any]] = []
    for cells in _components(np.isfinite(smoothed) & (smoothed >= threshold) & domain):
        ys = np.fromiter((cell[0] for cell in cells), dtype=int)
        xs = np.fromiter((cell[1] for cell in cells), dtype=int)
        peak_index = int(np.nanargmax(smoothed[ys, xs]))
        peak = float(smoothed[ys[peak_index], xs[peak_index]])
        if peak < peak_threshold:
            continue
        candidate_mask[ys, xs] = True
        candidates.append({
            "candidate_id": f"pmc-candidate-{len(candidates) + 1:04d}",
            "classification": "candidate",
            "confirmation_status": "not_confirmed",
            "pixel_count": int(len(cells)),
            "peak_850hpa_relative_vorticity_s-1": peak,
            "peak_pixel_rc": [int(ys[peak_index]), int(xs[peak_index])],
            "centroid_pixel_rc": [float(np.mean(ys)), float(np.mean(xs))],
        })
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
        "parameters": dict(PARAMETERS),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "satellite_comparison": compare_satellite(candidate_mask, satellite_observation_mask,
                                                    np.isfinite(smoothed) & domain,
                                                    satellite_observation_metadata,grid),
        "limitations": [
            "Кандидаты не являются подтверждёнными ПМЦ или прогнозом зарождения.",
            "Реализованы сглаживание и пороговые компоненты; трекинг, steering flow и опубликованные QC-процедуры отсутствуют.",
            "Спутниковое сравнение описывает только пиксельное перекрытие заданных масок и не является независимой метеорологической верификацией.",
            "Искажение масштаба конкретной проекции не оценивается; пригодность сетки для заданной широты требует отдельной проверки.",
        ],
    }
