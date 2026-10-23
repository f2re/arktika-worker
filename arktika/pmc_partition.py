"""Experimental raster peak partitioning for PMC candidate components.

This is a discrete contour approximation inspired by IMPMCT, not a complete
implementation of its contour interpolation and valley-line construction.
The output regions are candidate subdivisions, never confirmed PMCs.
"""
from __future__ import annotations

from math import floor
from typing import Any

import numpy as np
from scipy import ndimage


CONTOUR_INTERVAL_S_1 = 1.0e-6
ISOLATION_GAMMA = 0.15
CONNECTIVITY = np.ones((3, 3), dtype=np.uint8)


def partition_component(field: np.ndarray, component: np.ndarray,
                        peaks: list[dict[str, Any]], *, threshold: float,
                        grid: dict[str, Any], boundary_censored: bool) -> dict[str, Any]:
    """Partition a threshold component into experimental isolated-peak regions.

    Closed cores are discrete superlevel connected components at the first
    configured contour above each max-tree saddle. Remaining component cells
    are assigned to the nearest peak in projected grid coordinates. This
    assignment is a raster approximation; it does not reproduce published
    valley-line interpolation or geographic distance exactly.
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
    local_field = field[y0:y1, x0:x1]
    local_labels = np.zeros(local_component.shape, dtype=np.int16)
    seeds: list[tuple[int, int]] = []
    for peak in diagnostics:
        peak_row, peak_col = peak["peak_seed_pixel_rc"]
        seed = (int(peak_row) - y0, int(peak_col) - x0)
        saddle = float(peak["saddle_vorticity_s-1"])
        contour = (floor(saddle / CONTOUR_INTERVAL_S_1 + 1e-9) + 1) * CONTOUR_INTERVAL_S_1
        if (seed[0] < 0 or seed[1] < 0 or seed[0] >= local_component.shape[0]
                or seed[1] >= local_component.shape[1] or not local_component[seed]):
            labels[component] = 1
            return _single_region("contour_seed_unavailable_unpartitioned", labels, rows, cols)
        contour_mask = local_component & (local_field >= contour)
        contour_labels, _ = ndimage.label(contour_mask, structure=CONNECTIVITY)
        marker = int(contour_labels[seed])
        if marker == 0:
            labels[component] = 1
            return _single_region("contour_level_unavailable_unpartitioned", labels, rows, cols)
        core = contour_labels == marker
        seeds.append(seed)
        local_labels[core & (local_labels == 0)] = len(seeds)

    # Resolve any overlapping raster cores by nearest projected pixel centre.
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
    unassigned = local_labels[component_rows, component_cols] == 0
    local_labels[component_rows[unassigned], component_cols[unassigned]] = nearest[unassigned]
    labels[y0:y1, x0:x1] = local_labels
    regions = []
    for idx, peak in enumerate(diagnostics, start=1):
        region_mask = labels == idx
        regions.append(_region(idx, region_mask, peak, "discrete_contour_core_plus_nearest_remainder"))
    return {"status": "partitioned_experimental", "labels": labels,
            "regions": regions,
            "coverage_conserved": bool(np.array_equal(labels > 0, component)
                                        and sum(item["pixel_count"] for item in regions) == int(component.sum()))}


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
