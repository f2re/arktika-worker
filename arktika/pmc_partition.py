"""Experimental raster peak partitioning for PMC candidate components.

This is a discrete contour approximation inspired by IMPMCT, not a complete
implementation of its contour interpolation and valley-line construction.
The output regions are candidate subdivisions, never confirmed PMCs.
"""
from __future__ import annotations

from math import floor, isfinite
from typing import Any

import numpy as np

from .pmc_contours import trace_isolines


CONTOUR_INTERVAL_S_1 = 1.0e-6
ISOLATION_GAMMA = 0.15


def partition_component(field: np.ndarray, component: np.ndarray,
                        peaks: list[dict[str, Any]], *, threshold: float,
                        grid: dict[str, Any], boundary_censored: bool) -> dict[str, Any]:
    """Partition a threshold component into experimental isolated-peak regions.

    Closed cores use the first configured contour above each discrete max-tree
    saddle, traced with bilinear marching squares. Remaining component cells
    are assigned to the nearest peak in projected grid coordinates. Saddle
    detection and remainder allocation remain raster approximations; this does
    not reproduce the complete published IMPMCT method.
    """
    labels = np.zeros(component.shape, dtype=np.int16)
    rows, cols = np.nonzero(component)
    if not len(rows):
        return {"status": "empty", "labels": labels, "regions": [],
                "coverage_conserved": True}

    diagnostics = [peak for peak in peaks if peak.get("isolated_by_experimental_gamma") is True]
    if boundary_censored:
        labels[component] = 1
        return _single_region("boundary_censored_unpartitioned", labels, rows, cols)
    if not diagnostics:
        labels[component] = 1
        return _single_region("no_isolated_peak_unpartitioned", labels, rows, cols)
    if len(diagnostics) == 1:
        labels[component] = 1
        region = _region(1, labels == 1, diagnostics[0], "single_peak_expanded_to_component")
        return {"status": "single_peak_expanded", "labels": labels,
                "regions": [region], "coverage_conserved": True}

    y0, y1 = int(rows.min()), int(rows.max()) + 1
    x0, x1 = int(cols.min()), int(cols.max()) + 1
    local_component = component[y0:y1, x0:x1]
    local_labels = np.zeros(local_component.shape, dtype=np.int16)
    seeds: list[tuple[int, int]] = []
    cores: list[np.ndarray] = []
    for peak in diagnostics:
        peak_row, peak_col = peak["peak_seed_pixel_rc"]
        seed = (int(peak_row) - y0, int(peak_col) - x0)
        saddle = float(peak["saddle_vorticity_s-1"])
        contour = (floor(saddle / CONTOUR_INTERVAL_S_1 + 1e-9) + 1) * CONTOUR_INTERVAL_S_1
        if (seed[0] < 0 or seed[1] < 0 or seed[0] >= local_component.shape[0]
                or seed[1] >= local_component.shape[1] or not local_component[seed]):
            labels[component] = 1
            return _single_region("contour_seed_unavailable_unpartitioned", labels, rows, cols)
        core = _closed_contour_core(
            field, component, (int(peak_row), int(peak_col)), contour)
        if core is None:
            labels[component] = 1
            return _single_region("contour_trace_unresolved_unpartitioned", labels, rows, cols)
        local_core = core[y0:y1, x0:x1] & local_component
        if not local_core[seed]:
            labels[component] = 1
            return _single_region("contour_seed_unavailable_unpartitioned", labels, rows, cols)
        seeds.append(seed)
        cores.append(local_core)

    # Resolve overlapping raster cores by nearest projected pixel centre.
    transform = [float(value) for value in grid["transform"]]
    component_rows, component_cols = np.nonzero(local_component)
    rows_global, cols_global = component_rows + y0 + 0.5, component_cols + x0 + 0.5
    pixel_x = transform[0] * cols_global + transform[1] * rows_global + transform[2]
    pixel_y = transform[3] * cols_global + transform[4] * rows_global + transform[5]
    seed_x = np.array([transform[0] * (col + x0 + 0.5) + transform[1] * (row + y0 + 0.5) + transform[2]
                       for row, col in seeds])
    seed_y = np.array([transform[3] * (col + x0 + 0.5) + transform[4] * (row + y0 + 0.5) + transform[5]
                       for row, col in seeds])
    distances = ((pixel_x[None, :] - seed_x[:, None]) ** 2
                 + (pixel_y[None, :] - seed_y[:, None]) ** 2)
    nearest = np.argmin(distances, axis=0).astype(np.int16) + 1
    core_membership = np.stack(cores)[:, component_rows, component_cols]
    core_distances = np.where(core_membership, distances, np.inf)
    has_core = core_membership.any(axis=0)
    nearest_core = np.argmin(core_distances, axis=0).astype(np.int16) + 1
    assigned = np.where(has_core, nearest_core, nearest)
    local_labels[component_rows, component_cols] = assigned
    for label, (row, col) in enumerate(seeds, start=1):
        # A core must own its generating maximum even if another contour also
        # encloses that sample and equal-distance ties favor an earlier peak.
        local_labels[row, col] = label
    labels[y0:y1, x0:x1] = local_labels
    regions = []
    for idx, peak in enumerate(diagnostics, start=1):
        region_mask = labels == idx
        if not region_mask.any():
            labels[component] = 1
            return _single_region("empty_region_unpartitioned", labels, rows, cols)
        regions.append(_region(idx, region_mask, peak,
                               "interpolated_marching_squares_core_plus_nearest_remainder"))
    return {"status": "partitioned_experimental", "labels": labels,
            "regions": regions,
            "coverage_conserved": bool(np.array_equal(labels > 0, component)
                                        and sum(item["pixel_count"] for item in regions) == int(component.sum()))}


def _closed_contour_core(field: np.ndarray, component: np.ndarray,
                         seed: tuple[int, int], level: float) -> np.ndarray | None:
    """Rasterize the unique closed interpolated contour that encloses ``seed``.

    The contour is traced in sample-grid coordinates, then component sample
    locations inside its polygon become the core. Any open, incomplete,
    ambiguous, or multiply-enclosing geometry returns ``None`` so the caller
    can keep the connected component intact.
    """
    rows, cols = np.nonzero(component)
    if not len(rows):
        return None
    y0 = max(0, int(rows.min()) - 1)
    y1 = min(component.shape[0], int(rows.max()) + 2)
    x0 = max(0, int(cols.min()) - 1)
    x1 = min(component.shape[1], int(cols.max()) + 2)
    if y1 - y0 < 2 or x1 - x0 < 2:
        return None
    local = np.asarray(field[y0:y1, x0:x1], dtype=float)
    if not np.isfinite(local).all() or not isfinite(float(level)):
        return None
    traced = trace_isolines(local, float(level))
    if traced.get("status") != "closed":
        return None
    local_seed = (seed[0] - y0, seed[1] - x0)
    enclosing = [line["points"] for line in traced.get("lines", [])
                 if line.get("closed") and _contains_point(line["points"], local_seed)]
    if len(enclosing) != 1:
        return None
    points = enclosing[0]
    core = np.zeros(component.shape, dtype=bool)
    for row, col in zip(*np.nonzero(component)):
        if _contains_point(points, (int(row) - y0, int(col) - x0)):
            core[row, col] = True
    return core if core[seed] else None


def _contains_point(polygon: list[list[float]], point: tuple[int, int]) -> bool:
    """Ray-cast a sample-grid point against a closed row/column polygon."""
    if len(polygon) < 4:
        return False
    row, col = point
    inside = False
    for first, second in zip(polygon, polygon[1:]):
        first_row, first_col = float(first[0]), float(first[1])
        second_row, second_col = float(second[0]), float(second[1])
        if (first_row > row) != (second_row > row):
            crossing_col = ((second_col - first_col) * (row - first_row)
                            / (second_row - first_row) + first_col)
            if col < crossing_col:
                inside = not inside
    return inside


def _region(label: int, mask: np.ndarray, peak: dict[str, Any], allocation: str) -> dict[str, Any]:
    rows, cols = np.nonzero(mask)
    return {
        "label": label,
        "pixel_count": int(len(rows)),
        "centroid_pixel_rc": [float(np.mean(rows)), float(np.mean(cols))] if len(rows) else None,
        "peak_vorticity_s-1": float(peak["peak_vorticity_s-1"]),
        "peak_pixel_rc": peak["peak_pixel_rc"],
        "saddle_vorticity_s-1": peak["saddle_vorticity_s-1"],
        "relative_isolation_contrast": peak["relative_isolation_contrast"],
        "allocation": allocation,
        "classification": "candidate",
        "confirmation_status": "not_confirmed",
    }


def _single_region(status: str, labels: np.ndarray, rows: np.ndarray,
                   cols: np.ndarray) -> dict[str, Any]:
    return {"status": status, "labels": labels,
            "regions": [{"label": 1, "pixel_count": int(len(rows)),
                         "centroid_pixel_rc": [float(np.mean(rows)), float(np.mean(cols))],
                         "allocation": status, "classification": "candidate",
                         "confirmation_status": "not_confirmed"}],
            "coverage_conserved": True}
