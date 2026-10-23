"""Experimental cross-source agreement for unconfirmed PMC candidates.

Agreement is a comparison of detector outputs, not independent truth, a PMC
confirmation, or a genesis forecast. CARRA2 is ERA5-boundary-forced and is not
independent of ERA5.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import hypot, isfinite
from numbers import Real
from typing import Any

from pyproj import CRS

ALGORITHM_ID = "arktika.experimental_pmc_source_agreement"
ALGORITHM_VERSION = "0.1.0"
LIMITATIONS = [
    "Согласие источников — сравнение кандидатов, а не независимая истина или подтверждение ПМЦ.",
    "Совпадение кандидатов не оценивает место или время зарождения ПМЦ.",
    "CARRA2 использует граничное форсирование ERA5 и не является независимым от ERA5 источником.",
    "Неоднозначные many-to-many связи остаются неразрешёнными и не засчитываются как совпадения.",
    "Технические показатели согласия не являются метеорологической валидацией.",
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
    """Validate and normalize the full projected metric target grid."""
    if not isinstance(grid, dict):
        raise ValueError("grid metadata is required")
    required = ("crs", "width", "height", "resolution_x_m", "resolution_y_m", "transform")
    if any(k not in grid for k in required):
        raise ValueError("grid requires CRS, dimensions, metric resolutions and full affine transform")
    try:
        crs = CRS.from_user_input(grid["crs"])
        w, h = grid["width"], grid["height"]
        if (isinstance(w, bool) or isinstance(h, bool) or not isinstance(w, Real)
                or not isinstance(h, Real) or int(w) != w or int(h) != h or w < 1 or h < 1):
            raise ValueError
        width, height = int(w), int(h)
        dx, dy = float(grid["resolution_x_m"]), float(grid["resolution_y_m"])
        transform = tuple(float(v) for v in grid["transform"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("grid metadata is invalid") from exc
    if (not crs.is_projected or not crs.axis_info or
            any(abs(float(axis.unit_conversion_factor or 0) - 1.0) > 1e-9 for axis in crs.axis_info[:2])):
        raise ValueError("grid CRS must use projected metre axes")
    if (len(transform) != 6 or not all(isfinite(x) for x in transform)
            or width < 1 or height < 1 or not isfinite(dx) or not isfinite(dy)
            or dx <= 0 or dy <= 0):
        raise ValueError("grid dimensions, transform and metric resolutions must be finite and positive")
    a, b, _, d, e, _ = transform
    if a == 0 or e == 0 or abs(b) > 1e-12 or abs(d) > 1e-12:
        raise ValueError("target grid must have a nonsingular north-up affine transform")
    if abs(abs(a) - dx) > 1e-6 or abs(abs(e) - dy) > 1e-6:
        raise ValueError("target grid metric resolutions must match affine pixel sizes")
    return (crs.to_wkt(), width, height, dx, dy, transform)


def _xy(candidate: Any, key: tuple[Any, ...]) -> tuple[float, float]:
    if not isinstance(candidate, dict):
        raise ValueError("candidate entries must be objects")
    try:
        row, col = (float(x) for x in candidate["centroid_pixel_rc"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("each candidate requires centroid_pixel_rc [row, col]") from exc
    _, width, height, _, _, (a, b, c, d, e, f) = key
    if not isfinite(row) or not isfinite(col) or not (0 <= row < height and 0 <= col < width):
        raise ValueError("candidate centroid must be finite and within target grid bounds")
    col += 0.5
    row += 0.5
    return a * col + b * row + c, d * col + e * row + f


def compare_candidate_sources(
    frames: list[dict[str, Any]], *, expected_sources: list[str],
    max_distance_m: float = 180_000.0,
    expected_times: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Compare detections at exact common UTC frames on an identical grid.

    ``frames`` contains one object per available source/time, including an
    empty ``candidates`` list when a detector ran and found none. Expected
    sources make absent source/time combinations explicit as unknown. Time
    coverage is measured per source; candidate match fractions use each
    source's candidate count as their own denominator.
    """
    if not isinstance(frames, list) or not isinstance(expected_sources, list) or not expected_sources:
        raise ValueError("frames and a non-empty expected_sources list are required")
    if (any(not isinstance(s, str) or not s.strip() for s in expected_sources)
            or len(set(expected_sources)) != len(expected_sources)):
        raise ValueError("expected_sources must contain unique non-empty names")
    if isinstance(max_distance_m, bool) or not isinstance(max_distance_m, Real) or not isfinite(float(max_distance_m)) or max_distance_m <= 0:
        raise ValueError("max_distance_m must be a finite positive number")
    sources = list(expected_sources)
    by_time: dict[datetime, dict[str, dict[str, Any]]] = {}
    grid_keys: dict[tuple[datetime, str], tuple[Any, ...]] = {}
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError("each frame must be an object")
        source = frame.get("source")
        if source not in sources:
            raise ValueError("frame source must appear in expected_sources")
        timestamp = _utc(frame.get("valid_time"))
        if source in by_time.setdefault(timestamp, {}):
            raise ValueError("duplicate frame for source and exact UTC time")
        if not isinstance(frame.get("candidates"), list):
            raise ValueError("each frame must contain a candidates list")
        key = _grid_key(frame.get("grid"))
        for candidate in frame["candidates"]:
            _xy(candidate, key)
        by_time[timestamp][source] = frame
        grid_keys[(timestamp, source)] = key

    if expected_times is not None:
        if (not isinstance(expected_times, dict) or set(expected_times) != set(sources)
                or any(not isinstance(stamps, list) for stamps in expected_times.values())):
            raise ValueError("expected_times must map every declared source to a UTC timestamp list")
        schedule: dict[str, set[datetime]] = {source: set() for source in sources}
        for source, stamps in expected_times.items():
            for stamp in stamps:
                parsed = _utc(stamp)
                if parsed in schedule[source]:
                    raise ValueError("expected_times must not contain duplicate UTC timestamps")
                schedule[source].add(parsed)
        for timestamp in by_time:
            if not any(timestamp in values for values in schedule.values()):
                raise ValueError("candidate frame falls outside the declared expected_times")
        timeline = sorted(set().union(*schedule.values()))
    else:
        timeline = sorted(by_time)
        schedule = {source: set(timeline) for source in sources}

    source_coverage = {s: {"expected_frames": len(schedule[s]), "available_frames": 0,
                           "missing_frames": 0, "comparable_frames": 0,
                           "candidate_count": 0} for s in sources}
    comparisons: dict[str, dict[str, Any]] = {}
    for i, left in enumerate(sources):
        for right in sources[i + 1:]:
            comparisons[f"{left}|{right}"] = {
                "sources": [left, right], "comparable_frames": 0,
                "candidate_count_by_source": {left: 0, right: 0},
                "matched_candidate_count_by_source": {left: 0, right: 0},
                "match_fraction_by_source": {left: None, right: None},
                "unresolved_ambiguous_edges": 0,
            }

    outputs = []
    for timestamp in timeline:
        present = by_time.get(timestamp, {})
        stamp = timestamp.isoformat().replace("+00:00", "Z")
        for source in sources:
            if timestamp in schedule[source] and source in present:
                source_coverage[source]["available_frames"] += 1
                source_coverage[source]["candidate_count"] += len(present[source]["candidates"])
            elif timestamp in schedule[source]:
                source_coverage[source]["missing_frames"] += 1
        active_sources = [s for s in sources if timestamp in schedule[s]]
        if len(active_sources) < 2:
            outputs.append({"valid_time": stamp, "status": "not_common_expected_time",
                            "scheduled_sources": active_sources, "missing_sources": [],
                            "grid_status": "not_comparable", "pairs": []})
            continue
        missing = [s for s in active_sources if s not in present]
        keys = [grid_keys[(timestamp, s)] for s in active_sources if s in present]
        if missing:
            outputs.append({"valid_time": stamp, "status": "unknown_missing_source_or_timestamp",
                            "missing_sources": missing, "grid_status": "not_comparable", "pairs": []})
            continue
        if any(k != keys[0] for k in keys[1:]):
            outputs.append({"valid_time": stamp, "status": "uncomparable_grid_mismatch",
                            "missing_sources": [], "grid_status": "mismatch", "pairs": []})
            continue
        grid_key = keys[0]
        for source in active_sources:
            source_coverage[source]["comparable_frames"] += 1
        pair_outputs = []
        for left_index, left in enumerate(active_sources):
            for right in active_sources[left_index + 1:]:
                pair_id = f"{left}|{right}"
                pair = comparisons[pair_id]
                lc = present[left]["candidates"]
                rc = present[right]["candidates"]
                pair["comparable_frames"] += 1
                pair["candidate_count_by_source"][left] += len(lc)
                pair["candidate_count_by_source"][right] += len(rc)
                edges = []
                for li, lcan in enumerate(lc):
                    lx, ly = _xy(lcan, grid_key)
                    for ri, rcan in enumerate(rc):
                        rx, ry = _xy(rcan, grid_key)
                        dist = hypot(rx - lx, ry - ly)
                        if dist <= float(max_distance_m):
                            edges.append((li, ri, dist))
                ld: dict[int, int] = {}
                rd: dict[int, int] = {}
                for li, ri, _ in edges:
                    ld[li] = ld.get(li, 0) + 1
                    rd[ri] = rd.get(ri, 0) + 1
                matches = [(li, ri, dist) for li, ri, dist in edges if ld[li] == rd[ri] == 1]
                ambiguous = sum(1 for li, ri, _ in edges if ld[li] > 1 or rd[ri] > 1)
                pair["unresolved_ambiguous_edges"] += ambiguous
                pair["matched_candidate_count_by_source"][left] += len(matches)
                pair["matched_candidate_count_by_source"][right] += len(matches)
                pair_outputs.append({"sources": [left, right], "status": "comparable",
                                     "matches": [{"left_candidate_id": lc[li].get("candidate_id"),
                                                  "right_candidate_id": rc[ri].get("candidate_id"),
                                                  "distance_m": dist} for li, ri, dist in matches],
                                     "unresolved_ambiguous_edges": ambiguous})
        outputs.append({"valid_time": stamp, "status": "comparable", "missing_sources": [],
                        "grid_status": "identical_validated_projected_target_grid", "pairs": pair_outputs})

    for pair in comparisons.values():
        for source in pair["sources"]:
            denom = pair["candidate_count_by_source"][source]
            pair["match_fraction_by_source"][source] = (
                pair["matched_candidate_count_by_source"][source] / denom if denom else None)
    return {
        "algorithm_id": ALGORITHM_ID, "algorithm_version": ALGORITHM_VERSION,
        "maturity": "experimental", "product_type": "cross_source_candidate_agreement",
        "confirmation_status": "not_confirmed", "genesis_assessment": "not_provided",
        "max_distance_m": float(max_distance_m), "expected_sources": sources,
        "source_coverage": source_coverage, "pairwise_summary": comparisons,
        "frames": outputs, "limitations": list(LIMITATIONS),
    }
