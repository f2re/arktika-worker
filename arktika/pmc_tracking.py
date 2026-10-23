"""Experimental temporal linker for unconfirmed PMC candidate detections.

This module links detector components only. It does not calculate vorticity,
identify satellite cloud structures, or confirm a polar mesocyclone.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import hypot, isfinite
from numbers import Real
from typing import Any

from pyproj import CRS
from .product_schema import product_passport


ALGORITHM_ID = "arktika.experimental_pmc_candidate_linker"
ALGORITHM_VERSION = "0.2.0"
ASSOCIATION_LIMIT_M = 180_000.0
LIMITATIONS = [
    "Трек связывает только экспериментальные кандидаты и не подтверждает ПМЦ.",
    "Начало траектории наблюдалось в данных; это не оценка места или времени зарождения.",
    "Неоднозначные компоненты связей разрываются; продолжение разрешено только для однозначных пар 1:1.",
    "Алгоритм не воспроизводит IMPMCT и не выполняет расчёт вихревости или спутниковую детекцию.",
    "Весь входной ряд отклоняется, если соседние кадры разделены интервалом больше max_step_seconds.",
]


def _utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("valid_time must be an ISO-8601 UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("valid_time must be an ISO-8601 UTC string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("valid_time must include UTC offset +00:00")
    return parsed.astimezone(timezone.utc)


def _grid_key(grid: Any) -> tuple[Any, ...]:
    if not isinstance(grid, dict):
        raise ValueError("each frame must include grid metadata")
    keys = ("crs", "width", "height", "resolution_x_m", "resolution_y_m", "transform")
    if any(key not in grid for key in keys):
        raise ValueError("grid requires CRS, dimensions, metric resolutions and full affine transform")
    if any(isinstance(grid[key], bool) or not isinstance(grid[key], Real)
           for key in ("width", "height")):
        raise ValueError("grid width and height must be positive integers")
    try:
        if any(not isfinite(float(grid[key])) or float(grid[key]) != int(grid[key]) or int(grid[key]) < 1
               for key in ("width", "height")):
            raise ValueError("grid width and height must be positive integers")
        crs = CRS.from_user_input(grid["crs"])
        transform = tuple(float(v) for v in grid["transform"])
        width, height = int(grid["width"]), int(grid["height"])
        dx, dy = float(grid["resolution_x_m"]), float(grid["resolution_y_m"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid grid metadata") from exc
    if (not crs.is_projected or not crs.axis_info or
            any(abs(float(axis.unit_conversion_factor or 0) - 1.0) > 1e-9 for axis in crs.axis_info[:2])):
        raise ValueError("grid CRS must use projected metre axes")
    if len(transform) != 6 or not all(isfinite(v) for v in transform):
        raise ValueError("grid transform must contain six finite affine values")
    if width < 1 or height < 1 or not isfinite(dx) or not isfinite(dy) or dx <= 0 or dy <= 0:
        raise ValueError("grid dimensions and metric resolutions must be positive")
    return (crs.to_wkt(), width, height, dx, dy, transform)


def _point_m(candidate: dict[str, Any], transform: tuple[float, ...],
             width: int, height: int) -> tuple[float, float]:
    try:
        row, col = (float(v) for v in candidate["centroid_pixel_rc"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("each candidate requires centroid_pixel_rc [row, col]") from exc
    if not isfinite(row) or not isfinite(col) or not (0 <= row < height and 0 <= col < width):
        raise ValueError("candidate centroid must be finite and within grid bounds")
    a, b, c, d, e, f = transform
    # Pixel-centre offset is included for a correct georeferenced location.
    col += 0.5
    row += 0.5
    return a * col + b * row + c, d * col + e * row + f


def link_candidates(frames: list[dict[str, Any]], *, max_step_seconds: float = 3600.0,
                    max_distance_m: float = ASSOCIATION_LIMIT_M) -> dict[str, Any]:
    """Link only unambiguous adjacent candidate frames by metric distance.

    Frames must be chronological, unique UTC times, and share exact source and
    grid metadata. The complete input is rejected if any interval is greater
    than ``max_step_seconds``; links never bridge missing analysis frames.
    """
    if not isinstance(frames, list):
        raise ValueError("frames must be a list of detector outputs")
    def positive_number(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"{name} must be a finite positive number")
        numeric = float(value)
        if not isfinite(numeric) or numeric <= 0:
            raise ValueError(f"{name} must be a finite positive number")
        return numeric

    max_step_seconds = positive_number(max_step_seconds, "max_step_seconds")
    max_distance_m = positive_number(max_distance_m, "max_distance_m")
    if not frames:
        return _result([], [])
    if any(not isinstance(frame, dict) for frame in frames):
        raise ValueError("each frame must be a detector output object")

    parsed_times: list[datetime] = []
    first = frames[0]
    source = first.get("source")
    grid = first.get("grid")
    master_grid = _grid_key(grid)
    if not isinstance(grid, dict):
        raise ValueError("each frame must include grid metadata")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("frame source provenance is required")
    for frame in frames:
        if frame.get("source") != source or _grid_key(frame.get("grid")) != master_grid:
            raise ValueError("all frames must have exactly the same source and grid")
        parsed_times.append(_utc(frame.get("valid_time")))
        if not isinstance(frame.get("candidates"), list):
            raise ValueError("each frame must contain a candidates list")
    if any(b <= a for a, b in zip(parsed_times, parsed_times[1:])):
        raise ValueError("frame valid_time values must be unique and strictly increasing")
    if any((b - a).total_seconds() > max_step_seconds for a, b in zip(parsed_times, parsed_times[1:])):
        raise ValueError("frame gap exceeds the maximum one-step interval")

    transform = tuple(float(v) for v in grid["transform"])
    width, height = int(grid["width"]), int(grid["height"])
    tracks: list[dict[str, Any]] = []
    active: dict[int, dict[str, Any]] = {}
    next_track = 1
    associations: list[dict[str, Any]] = []
    for frame_index, (frame, timestamp) in enumerate(zip(frames, parsed_times)):
        candidates = frame["candidates"]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError("candidate entries must be objects")
            _point_m(candidate, transform, width, height)
        matches: list[tuple[int, int, float]] = []
        if frame_index == 0 or not active or not candidates:
            pass
        else:
            # Candidate order is deterministic for equal-distance tie breaks.
            distances = []
            for prev_index, prev in sorted(active.items()):
                for curr_index, curr in enumerate(candidates):
                    px, py = _point_m(prev["_candidate"], transform, width, height)
                    cx, cy = _point_m(curr, transform, width, height)
                    distance = hypot(cx - px, cy - py)
                    if distance <= max_distance_m:
                        distances.append((distance, prev_index, curr_index))
            prev_degree: dict[int, int] = {}
            curr_degree: dict[int, int] = {}
            prev_neighbors: dict[int, set[int]] = {}
            curr_neighbors: dict[int, set[int]] = {}
            for _, pi, ci in distances:
                prev_degree[pi] = prev_degree.get(pi, 0) + 1
                curr_degree[ci] = curr_degree.get(ci, 0) + 1
                prev_neighbors.setdefault(pi, set()).add(ci)
                curr_neighbors.setdefault(ci, set()).add(pi)

            # Continue only isolated 1:1 graph edges. A nearest-neighbour
            # choice inside a split/merge component invents trajectory
            # continuity unsupported by these centroid-only inputs.
            matches = [
                (pi, ci, distance) for distance, pi, ci in distances
                if prev_degree[pi] == 1 and curr_degree[ci] == 1
            ]

            ambiguous_prev = {pi for pi, degree in prev_degree.items() if degree > 1}
            ambiguous_curr = {ci for ci, degree in curr_degree.items() if degree > 1}
            unseen_prev = set(ambiguous_prev)
            unseen_curr = set(ambiguous_curr)
            while unseen_prev or unseen_curr:
                seed_prev = min(unseen_prev) if unseen_prev else None
                seed_curr = min(unseen_curr) if seed_prev is None else None
                component_prev: set[int] = set()
                component_curr: set[int] = set()
                pending_prev = [seed_prev] if seed_prev is not None else []
                pending_curr = [seed_curr] if seed_curr is not None else []
                while pending_prev or pending_curr:
                    while pending_prev:
                        pi = pending_prev.pop()
                        if pi in component_prev:
                            continue
                        component_prev.add(pi)
                        unseen_prev.discard(pi)
                        pending_curr.extend(prev_neighbors.get(pi, ()))
                    while pending_curr:
                        ci = pending_curr.pop()
                        if ci in component_curr:
                            continue
                        component_curr.add(ci)
                        unseen_curr.discard(ci)
                        pending_prev.extend(curr_neighbors.get(ci, ()))
                possible_links = [
                    {"previous_index": pi,
                     "previous_candidate_id": active[pi]["_candidate"].get("candidate_id"),
                     "current_index": ci,
                     "current_candidate_id": candidates[ci].get("candidate_id"),
                     "distance_m": distance}
                    for distance, pi, ci in sorted(distances, key=lambda item: (item[0], item[1], item[2]))
                    if pi in component_prev and ci in component_curr
                ]
                associations.append({
                    "time": timestamp.isoformat().replace("+00:00", "Z"),
                    "status": "unresolved_split_merge_association",
                    "previous_candidates": [
                        {"index": pi, "candidate_id": active[pi]["_candidate"].get("candidate_id")}
                        for pi in sorted(component_prev)],
                    "current_candidates": [
                        {"index": ci, "candidate_id": candidates[ci].get("candidate_id")}
                        for ci in sorted(component_curr)],
                    "possible_links": possible_links,
                    "action": "no_tracks_continued_or_created_from_ambiguous_links",
                })

        matched_current: set[int] = set()
        new_active: dict[int, dict[str, Any]] = {}
        for prev_index, curr_index, distance in matches:
            track = active[prev_index]["track"]
            candidate = candidates[curr_index]
            point = _point_m(candidate, transform, width, height)
            _append_point(track, frame, candidate, timestamp, point, distance)
            new_active[curr_index] = {"track": track, "_candidate": candidate}
            matched_current.add(curr_index)
        for curr_index, candidate in enumerate(candidates):
            if curr_index in matched_current:
                continue
            track = _new_track(next_track, frame_index == 0)
            next_track += 1
            _append_point(track, frame, candidate, timestamp,
                          _point_m(candidate, transform, width, height), None)
            tracks.append(track)
            new_active[curr_index] = {"track": track, "_candidate": candidate}
        active = new_active
    valid_period = {
        "start": parsed_times[0].isoformat().replace("+00:00", "Z"),
        "end": parsed_times[-1].isoformat().replace("+00:00", "Z"),
    }
    return _result(tracks, associations, source=source, grid=grid, valid_period=valid_period)


def _new_track(number: int, left_censored: bool) -> dict[str, Any]:
    return {
        "track_id": f"pmc-track-{number:04d}",
        "classification": "candidate_only",
        "confirmation_status": "not_confirmed",
        "track_start_observed": True,
        "left_censored": left_censored,
        "points": [],
    }


def _append_point(track: dict[str, Any], frame: dict[str, Any], candidate: dict[str, Any],
                  timestamp: datetime, xy: tuple[float, float], distance: float | None) -> None:
    track["points"].append({
        "valid_time": timestamp.isoformat().replace("+00:00", "Z"),
        "candidate_id": candidate.get("candidate_id"),
        "centroid_pixel_rc": list(candidate["centroid_pixel_rc"]),
        "centroid_projected_m_xy": [xy[0], xy[1]],
        "link_distance_m": distance,
    })


def _result(tracks: list[dict[str, Any]], associations: list[dict[str, Any]],
            source: str | None = None, grid: dict[str, Any] | None = None,
            valid_period: dict[str, str] | None = None) -> dict[str, Any]:
    result = {
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "maturity": "experimental",
        "source": source,
        "grid": grid,
        "classification": "candidate_only",
        "confirmation_status": "not_confirmed",
        "track_count": len(tracks),
        "tracks": tracks,
        "unresolved_associations": associations,
        "limitations": list(LIMITATIONS),
    }
    if source and grid and valid_period:
        result["passport"] = product_passport(
            product_id="arktika.pmc.candidate_tracks",
            algorithm_id=ALGORITHM_ID,
            algorithm_version=ALGORITHM_VERSION,
            maturity="experimental",
            quantity_kind="candidate_track_catalog",
            quantity="Каталог наблюдавшихся сегментов треков кандидатов ПМЦ",
            units="tracks",
            valid_period=valid_period,
            data_kind="analysis",
            grid=dict(grid),
            source_grids=[dict(grid)],
            nodata="Нет кадров или связей — это не наблюдавшееся отсутствие ПМЦ.",
            quality_control={"status": "experimental_linking",
                             "track_count": len(tracks),
                             "unresolved_association_count": len(associations)},
            sources=[{"source_id": source}],
            calibration_version="not_applicable",
            calibration_status="not_applicable",
            limitations=list(LIMITATIONS),
            uncertainty={"status": "not_assessed", "method": None},
        )
    return result
