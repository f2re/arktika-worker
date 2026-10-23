"""Deterministic experimental marching-squares contour geometry.

This module traces isolines of a scalar raster whose samples lie at integer
``(row, column)`` coordinates. It does not identify meteorological objects,
construct PMC valley lines, or assign cells to peaks. Ambiguous or incomplete
geometry is reported explicitly so callers can fail closed.
"""
from __future__ import annotations

from collections import defaultdict
from math import hypot, isfinite
from typing import Any

import numpy as np


_ENDPOINT_TOLERANCE = 1.0e-9
_DECIDER_TOLERANCE = 1.0e-14


# Corners proceed clockwise: top-left, top-right, bottom-right, bottom-left.
# Edges are top, right, bottom, left. Pairings for the ambiguous cases are
# selected by the bilinear asymptotic decider, not by an arbitrary case table.
_CASE_SEGMENTS: dict[int, tuple[tuple[int, int], ...]] = {
    1: ((3, 0),), 2: ((0, 1),), 3: ((3, 1),), 4: ((1, 2),),
    6: ((0, 2),), 7: ((3, 2),), 8: ((2, 3),), 9: ((0, 2),),
    11: ((1, 2),), 12: ((1, 3),), 13: ((0, 1),), 14: ((3, 0),),
}


def trace_isolines(values: np.ndarray, level: float,
                   valid_mask: np.ndarray | None = None) -> dict[str, Any]:
    """Trace all bilinearly interpolated isolines at ``level``.

    Returned line coordinates are ``[row, column]`` in sample-grid space.
    Only cells whose four samples are valid are processed. A contour touching
    the raster boundary or a missing-data edge is open and therefore cannot
    be consumed as a closed region boundary. Exact-level samples and
    asymptotic-decider ties are marked ambiguous rather than perturbed.

    Status values are ``closed``, ``open_contour``, ``ambiguous_saddle``,
    ``incomplete_data``, ``no_contour``, or ``invalid_input``. Lines are
    included for inspection even when the overall status fails closed.
    """
    array = np.asarray(values, dtype=float)
    try:
        contour_level = float(level)
    except (TypeError, ValueError):
        return _empty_result("invalid_input", "level must be finite")
    if (array.ndim != 2 or min(array.shape, default=0) < 2
            or not isfinite(contour_level)):
        return _empty_result("invalid_input", "values must be a finite-level 2-D grid of at least 2x2")

    if valid_mask is None:
        valid = np.isfinite(array)
    else:
        raw_mask = np.asarray(valid_mask)
        if raw_mask.shape != array.shape or raw_mask.dtype.kind not in "biu":
            return _empty_result("invalid_input", "valid_mask must be a same-shape boolean/integer array")
        if not np.isin(raw_mask, (0, 1)).all():
            return _empty_result("invalid_input", "valid_mask values must be 0/1")
        valid = raw_mask.astype(bool) & np.isfinite(array)

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    ambiguous_saddles = 0
    incomplete_cells = 0
    exact_level_cells = 0
    height, width = array.shape
    for row in range(height - 1):
        for col in range(width - 1):
            rc = ((row, col), (row, col + 1),
                  (row + 1, col + 1), (row + 1, col))
            if not all(valid[point] for point in rc):
                incomplete_cells += 1
                continue
            corner_values = np.asarray([array[point] for point in rc], dtype=float)
            relative = corner_values - contour_level
            # A contour through a sample vertex has a non-unique local
            # polyline topology. Do not invent a perturbation convention.
            if np.any(relative == 0.0):
                exact_level_cells += 1
                continue
            high = relative > 0.0
            case = sum((1 << index) for index, is_high in enumerate(high) if is_high)
            if case in (0, 15):
                continue

            edge_points = _edge_intersections(rc, relative)
            pairs: tuple[tuple[int, int], ...]
            if case in (5, 10):
                # For f(x,y)=a + b*x + c*y + d*x*y, the determinant
                # asymptotic decider is f00*f11 - f10*f01. Its sign selects
                # which diagonal corners share a bilinear superlevel region.
                determinant = relative[0] * relative[2] - relative[1] * relative[3]
                scale = max(float(np.max(np.abs(relative))) ** 2, 1.0e-300)
                if abs(float(determinant)) <= _DECIDER_TOLERANCE * scale:
                    ambiguous_saddles += 1
                    continue
                if determinant > 0:
                    pairs = ((0, 1), (2, 3))
                else:
                    pairs = ((0, 3), (1, 2))
            else:
                pairs = _CASE_SEGMENTS[case]

            for first_edge, second_edge in pairs:
                first = edge_points.get(first_edge)
                second = edge_points.get(second_edge)
                if first is None or second is None:
                    # This should be unreachable for non-degenerate cases;
                    # track it as ambiguity rather than silently dropping it.
                    ambiguous_saddles += 1
                    continue
                segments.append((first, second))

    lines, open_lines = _stitch_segments(segments)
    has_incomplete_data = incomplete_cells > 0
    touches_boundary = any(_touches_raster_boundary(line, (height, width)) for line in lines)
    if ambiguous_saddles or exact_level_cells:
        status = "ambiguous_saddle" if ambiguous_saddles else "ambiguous_vertex"
    elif open_lines or touches_boundary:
        status = "open_contour"
    elif has_incomplete_data:
        status = "incomplete_data"
    elif lines:
        status = "closed"
    else:
        status = "no_contour"

    return {
        "status": status,
        "level": contour_level,
        "coordinate_order": "row_column_sample_grid",
        "lines": [{"points": [[float(row), float(col)] for row, col in line],
                   "closed": _is_closed(line)} for line in lines],
        "diagnostics": {
            "segment_count": len(segments),
            "line_count": len(lines),
            "open_line_count": open_lines,
            "ambiguous_saddle_cells": ambiguous_saddles,
            "exact_level_cells": exact_level_cells,
            "incomplete_cells": incomplete_cells,
            "touches_raster_boundary": touches_boundary,
            "has_incomplete_data": incomplete_cells > 0,
        },
    }


def _edge_intersections(corners: tuple[tuple[int, int], ...],
                        relative: np.ndarray) -> dict[int, tuple[float, float]]:
    edge_vertices = ((0, 1), (1, 2), (3, 2), (0, 3))
    intersections: dict[int, tuple[float, float]] = {}
    for edge, (first, second) in enumerate(edge_vertices):
        left, right = float(relative[first]), float(relative[second])
        if (left > 0) == (right > 0):
            continue
        fraction = left / (left - right)
        row0, col0 = corners[first]
        row1, col1 = corners[second]
        intersections[edge] = (row0 + fraction * (row1 - row0),
                               col0 + fraction * (col1 - col0))
    return intersections


def _stitch_segments(segments: list[tuple[tuple[float, float],
                                          tuple[float, float]]]
                     ) -> tuple[list[list[tuple[float, float]]], int]:
    """Join segment endpoints using a deterministic spatial hash."""
    if not segments:
        return [], 0

    def key(point: tuple[float, float]) -> tuple[int, int]:
        return (round(point[0] / _ENDPOINT_TOLERANCE),
                round(point[1] / _ENDPOINT_TOLERANCE))

    adjacency: dict[tuple[int, int], list[int]] = defaultdict(list)
    endpoints: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for index, segment in enumerate(segments):
        endpoints.append(segment)
        adjacency[key(segment[0])].append(index)
        adjacency[key(segment[1])].append(index)

    unused = set(range(len(segments)))
    lines: list[list[tuple[float, float]]] = []
    while unused:
        first_index = min(unused)
        first_segment = endpoints[first_index]
        start = min(first_segment, key=key)
        current = start
        line = [start]
        while True:
            incident = [index for index in adjacency[key(current)] if index in unused]
            if not incident:
                break
            segment_index = min(incident)
            unused.remove(segment_index)
            endpoint_a, endpoint_b = endpoints[segment_index]
            following = endpoint_b if key(endpoint_a) == key(current) else endpoint_a
            if key(following) == key(start):
                line.append(start)
                break
            line.append(following)
            current = following
        lines.append(line)

    open_count = sum(not _is_closed(line) for line in lines)
    lines.sort(key=lambda line: (key(line[0]), len(line), key(line[-1])))
    return lines, open_count


def _is_closed(line: list[tuple[float, float]]) -> bool:
    return len(line) > 2 and _same_point(line[0], line[-1])


def _same_point(first: tuple[float, float], second: tuple[float, float]) -> bool:
    return hypot(first[0] - second[0], first[1] - second[1]) <= _ENDPOINT_TOLERANCE


def _touches_raster_boundary(line: list[tuple[float, float]],
                             shape: tuple[int, int]) -> bool:
    max_row, max_col = shape[0] - 1, shape[1] - 1
    return any(row <= _ENDPOINT_TOLERANCE or col <= _ENDPOINT_TOLERANCE
               or row >= max_row - _ENDPOINT_TOLERANCE
               or col >= max_col - _ENDPOINT_TOLERANCE
               for row, col in line)


def _empty_result(status: str, reason: str) -> dict[str, Any]:
    return {"status": status, "lines": [], "diagnostics": {"reason": reason}}
