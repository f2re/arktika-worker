"""Durable-report helpers for retrospective PMC candidate archives.

This module links detections only. It never treats an empty or missing frame as
evidence that a cyclone was absent, and it never infers a genesis location.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from numbers import Real
from typing import Any

from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from math import floor, ceil
import numpy as np
from .geo import grid as map_grid
from .reanalysis.catalog import validate_area

from .pmc_tracking import link_candidates
from .pmc_source_agreement import compare_candidate_sources
from .product_schema import product_passport


ALGORITHM_ID = "arktika.experimental_pmc_archive_run"
ALGORITHM_VERSION = "0.5.0"
MAX_FIELDS = 128
MAX_GRID_PIXELS = 1_048_576


def validate_grid(value: Any) -> dict[str, Any]:
    """Validate and normalize the explicit projected target grid."""
    keys = {"crs", "width", "height", "transform", "resolution_x_m", "resolution_y_m"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Целевая сетка должна содержать только CRS, размеры, разрешение и affine transform.")
    width, height = value["width"], value["height"]
    if (isinstance(width, bool) or not isinstance(width, int) or width < 1
            or isinstance(height, bool) or not isinstance(height, int) or height < 1
            or width * height > MAX_GRID_PIXELS):
        raise ValueError("Размер целевой сетки должен быть положительным и не превышать 1 048 576 пикселей.")
    if not isinstance(value["crs"], str) or len(value["crs"]) > 256:
        raise ValueError("Укажите CRS целевой сетки.")
    try:
        crs = CRS.from_user_input(value["crs"])
        dx, dy = float(value["resolution_x_m"]), float(value["resolution_y_m"])
        transform = [float(item) for item in value["transform"]]
    except (CRSError, TypeError, ValueError, OverflowError):
        raise ValueError("Неверные CRS, разрешение или affine transform целевой сетки.") from None
    if (not crs.is_projected or not crs.axis_info
            or any(abs(float(axis.unit_conversion_factor or 0) - 1.0) > 1e-9
                   for axis in crs.axis_info[:2])):
        raise ValueError("Целевая CRS должна быть проецированной системой с осями в метрах.")
    if (not isfinite(dx) or not isfinite(dy) or dx <= 0 or dy <= 0
            or len(transform) != 6 or not all(isfinite(item) for item in transform)):
        raise ValueError("Разрешение должно быть положительным, affine transform — содержать шесть конечных чисел.")
    if (transform[0] <= 0 or transform[4] >= 0 or transform[1] != 0 or transform[3] != 0
            or abs(transform[0] - dx) > max(dx, 1.0) * 1e-9
            or abs(-transform[4] - dy) > max(dy, 1.0) * 1e-9):
        raise ValueError("Целевая сетка должна быть невырожденной, неповёрнутой и согласованной с разрешением.")
    return {"crs": crs.to_string(), "width": width, "height": height,
            "transform": transform, "resolution_x_m": dx, "resolution_y_m": dy}


def target_grid_for_area(*, preset: str, width: int, area: Any) -> dict[str, Any]:
    """Crop a projected map grid to the selected geographic rectangle."""
    north, west, south, east = validate_area(area)
    if west > east:
        raise ValueError("Область через антимеридиан сейчас не поддерживается.")
    if isinstance(width, bool) or not isinstance(width, int) or not 256 <= width <= 2048:
        raise ValueError("Размер целевой сетки должен быть в диапазоне 256–2048 пикселей.")
    base = map_grid(preset, width)
    left, bottom, right, top = base["bounds"]
    transformer = Transformer.from_crs("EPSG:4326", base["crs"], always_xy=True)
    longitudes = np.linspace(west, east, 257)
    latitudes = np.linspace(south, north, 257)
    boundary_lon = np.concatenate((longitudes, longitudes, np.full(257, west), np.full(257, east)))
    boundary_lat = np.concatenate((np.full(257, south), np.full(257, north), latitudes, latitudes))
    x, y = transformer.transform(boundary_lon, boundary_lat)
    x = np.asarray(x, dtype=float); y = np.asarray(y, dtype=float)
    finite = np.isfinite(x) & np.isfinite(y)
    if not finite.any():
        raise ValueError("Регион не пересекает проекцию целевой карты.")
    crop_left=max(left,float(x[finite].min())); crop_right=min(right,float(x[finite].max()))
    crop_bottom=max(bottom,float(y[finite].min())); crop_top=min(top,float(y[finite].max()))
    if crop_left >= crop_right or crop_bottom >= crop_top:
        raise ValueError("Регион не пересекает проекцию целевой карты.")
    dx=(right-left)/base["width"];dy=(top-bottom)/base["height"]
    col0=max(0,floor((crop_left-left)/dx));col1=min(base["width"],ceil((crop_right-left)/dx))
    row0=max(0,floor((top-crop_top)/dy));row1=min(base["height"],ceil((top-crop_bottom)/dy))
    cropped_width=max(1,col1-col0);cropped_height=max(1,row1-row0)
    return validate_grid({"crs":base["crs"],"width":cropped_width,"height":cropped_height,
        "transform":[dx,0,left+col0*dx,0,-dy,top-row0*dy],
        "resolution_x_m":dx,"resolution_y_m":dy})


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Срок архива должен быть строкой ISO 8601 в UTC.") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("Срок архива должен содержать пояс UTC.")
    return parsed.astimezone(timezone.utc)


def _candidate_emergence_evidence(
    track: dict[str, Any],
    *,
    source: str,
    expected_times: list[str],
    frame_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe the archive evidence around a track's first model detection.

    This records an observed candidate-screen transition only. It cannot infer
    physical genesis, and a missing or failed previous expected frame remains
    unknown rather than a clear analysis.
    """
    points = track.get("points")
    if not isinstance(points, list) or not points or not isinstance(points[0], dict):
        raise ValueError("candidate track must contain a first observed point")
    first_point = points[0]
    onset = _utc(first_point.get("valid_time"))
    onset_stamp = onset.isoformat().replace("+00:00", "Z")
    normalized_expected = [_utc(stamp) for stamp in expected_times]
    schedule_has_duplicates = len(normalized_expected) != len(set(normalized_expected))
    expected = sorted(set(normalized_expected))
    prior_stamps = [stamp for stamp in expected if stamp < onset]
    previous_expected = prior_stamps[-1] if prior_stamps else None
    onset_is_scheduled = onset in expected

    completed: dict[datetime, dict[str, Any]] = {}
    failed: set[datetime] = set()
    ambiguous: set[datetime] = set()
    invalid: set[datetime] = set()
    seen_records: set[datetime] = set()
    for row in frame_records:
        if row.get("source") != source:
            continue
        stamp_value = row.get("valid_time")
        try:
            stamp = _utc(stamp_value)
        except (TypeError, ValueError):
            continue
        if stamp in seen_records:
            completed.pop(stamp, None)
            failed.discard(stamp)
            invalid.discard(stamp)
            ambiguous.add(stamp)
            continue
        seen_records.add(stamp)
        result = row.get("result")
        if row.get("status") == "completed" and isinstance(result, dict):
            completed[stamp] = result
        elif row.get("status") == "error":
            failed.add(stamp)
        else:
            invalid.add(stamp)

    if schedule_has_duplicates:
        prior_frame = {
            "status": "unknown_duplicate_expected_schedule_time",
            "valid_time": None,
            "candidate_count": None,
        }
    elif not expected:
        prior_frame = {
            "status": "unknown_expected_schedule_unavailable",
            "valid_time": None,
            "candidate_count": None,
        }
    elif not onset_is_scheduled:
        prior_frame = {
            "status": "unknown_onset_outside_declared_schedule",
            "valid_time": (previous_expected.isoformat().replace("+00:00", "Z")
                           if previous_expected else None),
            "candidate_count": None,
        }
    elif previous_expected is None:
        prior_frame = {
            "status": "left_censored_at_archive_window_start",
            "valid_time": None,
            "candidate_count": None,
        }
    else:
        prior_stamp = previous_expected.isoformat().replace("+00:00", "Z")
        previous = completed.get(previous_expected)
        if previous_expected in ambiguous:
            prior_frame = {"status": "unknown_ambiguous_previous_expected_frame",
                           "valid_time": prior_stamp, "candidate_count": None}
        elif previous_expected in invalid:
            prior_frame = {"status": "unknown_invalid_previous_expected_frame_record",
                           "valid_time": prior_stamp, "candidate_count": None}
        elif previous is not None:
            prior_candidates = previous.get("candidates")
            if not isinstance(prior_candidates, list):
                status = "unknown_previous_processed_frame_invalid_candidates"
                candidate_count = None
            else:
                status = ("previous_processed_frame_had_no_model_candidates"
                          if not prior_candidates else "previous_processed_frame_had_candidates")
                candidate_count = len(prior_candidates)
            prior_frame = {"status": status, "valid_time": prior_stamp,
                           "candidate_count": candidate_count}
        else:
            status = ("unknown_previous_expected_frame_failed"
                      if previous_expected in failed
                      else "unknown_previous_expected_frame_missing")
            prior_frame = {"status": status, "valid_time": prior_stamp,
                           "candidate_count": None}

    current_result = completed.get(onset, {})
    satellite = current_result.get("satellite_comparison")
    satellite_status = (satellite.get("status") if isinstance(satellite, dict)
                        else "not_supplied")
    if onset in ambiguous:
        satellite_status = "unknown_ambiguous_frame_records"
    elif onset in invalid:
        satellite_status = "unknown_invalid_frame_record"
    if not isinstance(satellite_status, str):
        satellite_status = "unknown_invalid_comparison_status"

    return {
        "source": source,
        "track_id": track.get("track_id"),
        "candidate_id": first_point.get("candidate_id"),
        "first_detected_time": onset_stamp,
        "onset_in_expected_schedule": onset_is_scheduled if expected else None,
        "left_censored": bool(track.get("left_censored")),
        "prior_expected_steps_in_study_window": len(prior_stamps),
        "prior_frame": prior_frame,
        "satellite_comparison_status_at_first_detection": satellite_status,
        "genesis_assessment": "not_assessed",
        "interpretation": (
            "Первая фиксация кандидата модельным экраном не является физическим "
            "зарождением ПМЦ; спутниковое подтверждение и genesis здесь не оцениваются."
        ),
    }


def _onset_cross_source_evidence(
    emergence: dict[str, Any], agreement: dict[str, Any] | None,
) -> dict[str, Any]:
    """Link exact-time source matches to a model candidate's first detection."""
    if agreement is None:
        return {"status": "not_applicable_single_source", "pairs": []}
    stamp = emergence.get("first_detected_time")
    source = emergence.get("source")
    candidate_id = emergence.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        return {"status": "unknown_onset_candidate_id_missing", "pairs": []}
    frames = agreement.get("frames")
    if not isinstance(frames, list):
        return {"status": "unknown_comparison_unavailable", "pairs": []}
    exact = [row for row in frames if isinstance(row, dict)
             and row.get("valid_time") == stamp]
    if len(exact) != 1:
        return {"status": "unknown_exact_time_frame_missing_or_ambiguous", "pairs": []}
    frame = exact[0]
    if frame.get("status") != "comparable":
        return {"status": frame.get("status", "unknown_invalid_comparison_frame"),
                "missing_sources": frame.get("missing_sources", []), "pairs": []}
    pair_rows = frame.get("pairs")
    if not isinstance(pair_rows, list):
        return {"status": "unknown_invalid_pair_list", "pairs": []}
    relevant = []
    for pair in pair_rows:
        if not isinstance(pair, dict) or source not in pair.get("sources", []):
            continue
        sources = pair.get("sources", [])
        matches = pair.get("matches")
        if not isinstance(matches, list):
            continue
        source_is_left = sources[0] == source
        matched = []
        for item in matches:
            if not isinstance(item, dict):
                continue
            own_key = "left_candidate_id" if source_is_left else "right_candidate_id"
            other_key = "right_candidate_id" if source_is_left else "left_candidate_id"
            if item.get(own_key) == candidate_id:
                matched.append({"candidate_id": item.get(other_key),
                                "distance_m": item.get("distance_m")})
        other_source = sources[1] if source_is_left else sources[0]
        relevant.append({"other_source": other_source,
                         "status": "matched_candidate" if matched else "no_spatial_match_within_radius",
                         "matches": matched})
    if not relevant:
        return {"status": "unknown_source_pair_not_comparable", "pairs": []}
    return {"status": "compared_exact_utc", "pairs": relevant}


def _onset_satellite_evidence(
    emergence: dict[str, Any], satellite_frames: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attach only the exact source/time observation comparison, if available."""
    source = emergence.get("source")
    stamp = emergence.get("first_detected_time")
    exact = []
    for row in satellite_frames:
        if not isinstance(row, dict) or row.get("source") != source:
            continue
        try:
            row_stamp = _utc(row.get("valid_time")).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            continue
        if row_stamp == stamp:
            exact.append(row)
    if len(exact) != 1:
        return {"status": "unknown_exact_time_comparison_missing_or_ambiguous"}
    row = exact[0]
    result = {"status": row.get("status", "unknown_invalid_comparison_status")}
    if row.get("status") == "comparison_only":
        result.update({key: row[key] for key in ("observation", "coverage", "metrics")
                       if key in row})
    elif isinstance(row.get("reason_code"), str):
        result["reason_code"] = row["reason_code"]
    return result
def build_archive_report(*, task_id: str, plan: dict[str, Any],
                         expected_times: dict[str, list[str]],
                         interval_hours: dict[str, int],
                         frame_records: list[dict[str, Any]],
                         cached_fields: list[dict[str, Any]],
                         target_grid: dict[str, Any] | None = None,
                         satellite_comparisons: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Summarize completed cached frames and link only contiguous sequences."""
    successful = [row for row in frame_records if row.get("status") == "completed"
                  and isinstance(row.get("result"), dict)]
    errors = [row for row in frame_records if row.get("status") == "error"]
    frame_by_source: dict[str, list[dict[str, Any]]] = {}
    for row in successful:
        frame = row["result"]
        frame_by_source.setdefault(frame["source"], []).append(frame)

    coverage: list[dict[str, Any]] = []
    track_rows: list[dict[str, Any]] = []
    for source in plan["sources"]:
        expected = expected_times.get(source, [])
        expected_set = set(expected)
        source_frames = sorted(frame_by_source.get(source, []), key=lambda item: item["valid_time"])
        completed_times = {item["valid_time"] for item in source_frames}
        cached_times = {str(item.get("time", "")).replace("+00:00", "Z")
                        for item in cached_fields
                        if item.get("source") == source and item.get("variable") == "wind"
                        and item.get("level") == 850 and
                        str(item.get("time", "")).replace("+00:00", "Z") in expected_set}
        source_errors = sum(1 for row in errors if row.get("source") == source)
        missing = [stamp for stamp in expected if stamp not in completed_times]
        coverage.append({
            "source": source,
            "expected_steps": len(expected),
            "cached_steps": len(cached_times),
            "selected_steps": sum(1 for row in frame_records if row.get("source") == source),
            "processed_steps": len(completed_times),
            "failed_steps": source_errors,
            "unprocessed_steps": len(missing),
            "unprocessed_preview_utc": missing[:24],
            "unprocessed_preview_truncated": len(missing) > 24,
        })

        if not source_frames:
            track_rows.append({"source": source, "track_count": 0, "tracks": [],
                               "unresolved_associations": [],
                               "candidate_emergence_evidence": []})
            continue
        cadence_hours = interval_hours[source]
        cadence_seconds = float(cadence_hours) * 3600
        segments: list[list[dict[str, Any]]] = []
        segment: list[dict[str, Any]] = []
        previous: datetime | None = None
        for frame in source_frames:
            stamp = _utc(frame["valid_time"])
            if previous is not None and (stamp - previous).total_seconds() != cadence_seconds:
                if segment:
                    segments.append(segment)
                segment = []
            segment.append(frame)
            previous = stamp
        if segment:
            segments.append(segment)

        tracks: list[dict[str, Any]] = []
        associations: list[dict[str, Any]] = []
        next_track = 1
        for sequence_id, contiguous in enumerate(segments, start=1):
            linked = link_candidates(contiguous, max_step_seconds=cadence_seconds)
            for track in linked["tracks"]:
                track["track_id"] = f"{source}-segment-{sequence_id:03d}-track-{next_track:04d}"
                next_track += 1
                tracks.append(track)
            associations.extend(linked["unresolved_associations"])
        emergence_evidence = [
            _candidate_emergence_evidence(
                track, source=source, expected_times=expected,
                frame_records=frame_records,
            )
            for track in tracks
        ]
        track_rows.append({"source": source, "track_count": len(tracks), "tracks": tracks,
                           "unresolved_associations": associations,
                           "candidate_emergence_evidence": emergence_evidence,
                           "limitations": linked["limitations"] if segments else []})

    incomplete = any(item["unprocessed_steps"] or item["failed_steps"] for item in coverage)
    source_agreement = None
    if len(plan["sources"]) > 1:
        agreement_frames = []
        for row in successful:
            result = row["result"]
            if isinstance(result.get("source"), str) and isinstance(result.get("valid_time"), str):
                agreement_frames.append({
                    "source": result["source"],
                    "valid_time": result["valid_time"],
                    "grid": result.get("grid"),
                    "candidates": result.get("candidates", []),
                })
        try:
            source_agreement = compare_candidate_sources(
                agreement_frames, expected_sources=plan["sources"],
                expected_times=expected_times)
        except (TypeError, ValueError) as exc:
            # The report remains useful when one source cannot participate in
            # an exact-grid comparison, but never guesses coordinates or links.
            source_agreement = {
                "status": "unavailable_invalid_candidate_metadata",
                "reason": str(exc),
                "maturity": "experimental",
                "confirmation_status": "not_confirmed",
                "genesis_assessment": "not_provided",
            }
    if target_grid is None:
        target_grid = next((row["result"].get("grid") for row in successful
                            if isinstance(row["result"].get("grid"), dict)), None)
    # The single-time detector performs the pixel comparison while the mask is
    # available. Archive frames retain that compact result, never the raw mask.
    # Accept legacy single-time observations only at exact UTC; interval
    # observations only when the model instant lies inside the declared scan.
    # Stale, ambiguous or malformed comparisons remain unknown.
    satellite_frames: list[dict[str, Any]] = []
    aggregate = {key: 0 for key in ("true_positive_pixels", "false_positive_pixels",
                                    "false_negative_pixels", "true_negative_pixels")}
    observed_count = 0
    listed_pairs: set[tuple[str, str]] = set()
    comparisons_by_frame: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in satellite_comparisons or []:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        try:
            stamp = _utc(entry.get("valid_time")).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            continue
        if isinstance(source, str):
            comparisons_by_frame[(source, stamp)] = entry
    for row in frame_records:
        if row.get("status") != "completed" or not isinstance(row.get("result"), dict):
            continue
        result = row["result"]
        try:
            stamp = _utc(row.get("valid_time") or result.get("valid_time")).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            continue
        entry = comparisons_by_frame.get((row.get("source"), stamp))
        if entry is None:
            continue
        comparison = entry.get("comparison")
        if not isinstance(comparison, dict) or comparison.get("status") == "not_supplied":
            continue
        listed_pairs.add((row.get("source"), stamp))
        observation = comparison.get("observation")
        valid_match = False
        if isinstance(observation, dict):
            try:
                observation_time = _utc(observation.get("valid_time"))
                frame_time = _utc(stamp)
                scan_start = observation.get("scan_start_utc")
                scan_end = observation.get("scan_end_utc")
                if scan_start is None and scan_end is None:
                    temporal_match = observation_time == frame_time
                elif isinstance(scan_start, str) and isinstance(scan_end, str):
                    start_time = _utc(scan_start)
                    end_time = _utc(scan_end)
                    temporal_match = (start_time < end_time
                                      and start_time <= observation_time <= end_time
                                      and start_time <= frame_time <= end_time)
                else:
                    temporal_match = False
                valid_match = (
                    all(isinstance(observation.get(key), str) and observation[key].strip()
                        for key in ("source", "product_id", "observation_id"))
                    and temporal_match
                    and isinstance(target_grid, dict)
                    and validate_grid(result.get("grid")) == validate_grid(target_grid)
                    and validate_grid(entry.get("grid")) == validate_grid(target_grid)
                    and validate_grid(observation.get("grid")) == validate_grid(entry.get("grid"))
                )
            except (TypeError, ValueError):
                valid_match = False
        metrics = comparison.get("metrics")
        if not valid_match or comparison.get("status") != "comparison_only" or not isinstance(metrics, dict):
            status = comparison.get("status")
            if status not in ("unknown_no_eligible_observation", "unknown_ambiguous_observations"):
                status = "unknown_unmatched"
            frame = {"source": row.get("source"), "valid_time": row.get("valid_time"),
                     "status": status, "metrics": None}
            reason_code = comparison.get("reason_code")
            if reason_code in {"duplicate_exact_time_observations", "ambiguous_eligible_observations",
                               "no_eligible_observation", "observation_grid_unavailable"}:
                frame["reason_code"] = reason_code
            satellite_frames.append(frame)
            continue
        counts = {key: metrics.get(key) for key in aggregate}
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in counts.values()):
            satellite_frames.append({"source": row.get("source"), "valid_time": row.get("valid_time"),
                                     "status": "unknown_unmatched", "metrics": None})
            continue
        for key, value in counts.items():
            aggregate[key] += value
        observed_count += 1
        satellite_frames.append({"source": row.get("source"), "valid_time": row.get("valid_time"),
                                 "status": "comparison_only", "observation": observation,
                                 "metrics": dict(metrics), "coverage": comparison.get("coverage")})
    for source, stamps in expected_times.items():
        for expected_stamp in stamps:
            try:
                stamp = _utc(expected_stamp).isoformat().replace("+00:00", "Z")
            except (TypeError, ValueError):
                continue
            pair = (source, stamp)
            if pair not in listed_pairs:
                satellite_frames.append({"source": source, "valid_time": stamp,
                                         "status": "unknown_not_compared", "metrics": None})
    expected_pair_count = sum(len(stamps) for stamps in expected_times.values())
    if observed_count:
        satellite_status = "comparison_only" if observed_count == expected_pair_count else "partial_comparison"
        tp, fp, fn = (aggregate["true_positive_pixels"], aggregate["false_positive_pixels"],
                      aggregate["false_negative_pixels"])
        satellite_metrics = {
            **aggregate,
            "intersection_over_union": tp / (tp + fp + fn) if tp + fp + fn else None,
            "probability_of_detection": tp / (tp + fn) if tp + fn else None,
            "false_alarm_ratio": fp / (tp + fp) if tp + fp else None,
        }
    else:
        satellite_status = "not_supplied"
        satellite_metrics = None
    for row in track_rows:
        for evidence in row["candidate_emergence_evidence"]:
            evidence["cross_source_evidence_at_first_detection"] = (
                _onset_cross_source_evidence(evidence, source_agreement))
            evidence["satellite_evidence_at_first_detection"] = (
                _onset_satellite_evidence(evidence, satellite_frames))
    passport = None
    if isinstance(target_grid, dict):
        # The product passport describes only successfully processed frames.
        # The requested study interval remains available in the plan/coverage,
        # but must not make a partial product appear to span missing times.
        actual_times: list[datetime] = []
        for row in successful:
            result = row["result"]
            stamp = result.get("valid_time") or row.get("valid_time")
            try:
                actual_times.append(_utc(stamp))
            except (TypeError, ValueError):
                continue
        period_start = min(actual_times).isoformat().replace("+00:00", "Z") if actual_times else None
        period_end = max(actual_times).isoformat().replace("+00:00", "Z") if actual_times else None
        period = ({"start": _utc(period_start).isoformat().replace("+00:00", "Z"),
                   "end": _utc(period_end).isoformat().replace("+00:00", "Z")}
                  if period_start and period_end else None)
        source_refs: list[dict[str, Any]] = []
        source_grids: list[dict[str, Any]] = []
        seen_sources: set[tuple[str, str]] = set()
        seen_grids: set[str] = set()
        for row in successful:
            result = row["result"]
            source = result.get("source")
            field_passport = result.get("field_passport")
            refs = field_passport.get("sources", []) if isinstance(field_passport, dict) else []
            source_ref_count = len(source_refs)
            if isinstance(refs, list):
                for item in refs:
                    if not isinstance(item, dict):
                        continue
                    source_id = item.get("source_id")
                    field_id = item.get("field_id")
                    if not isinstance(source_id, str) or not source_id.strip():
                        continue
                    identity = (source_id.strip(), field_id.strip() if isinstance(field_id, str) else "")
                    if identity not in seen_sources:
                        source_refs.append({"source_id": identity[0], **({"field_id": identity[1]} if identity[1] else {})})
                        seen_sources.add(identity)
            if len(source_refs) == source_ref_count and isinstance(source, str) and source.strip():
                identity = (source.strip(), "")
                if identity not in seen_sources:
                    source_refs.append({"source_id": identity[0]})
                    seen_sources.add(identity)
            grids = field_passport.get("source_grids", []) if isinstance(field_passport, dict) else []
            source_grid_count = len(source_grids)
            if isinstance(grids, list):
                for source_grid in grids:
                    if isinstance(source_grid, dict):
                        key = repr(sorted(source_grid.items()))
                        if key not in seen_grids:
                            source_grids.append(source_grid)
                            seen_grids.add(key)
            # A frame's declared grid is evidence for the grid on which its
            # candidates were detected. Do not label the archive target grid as
            # an upstream source grid unless a frame actually declares it.
            frame_grid = result.get("grid")
            if len(source_grids) == source_grid_count and isinstance(frame_grid, dict):
                key = repr(sorted(frame_grid.items()))
                if key not in seen_grids:
                    source_grids.append({"role": "candidate_detection_grid", **frame_grid})
                    seen_grids.add(key)
        report_limitations = [
            "Отсутствующие, неполные и ошибочные сроки не трактуются как отсутствие ПМЦ.",
            "Треки строятся только внутри непрерывных последовательностей наблюдавшихся модельных кандидатов.",
            "Журнал появления описывает только первый вывод модельного экрана и предшествующие кадры; физическое время и место genesis не оцениваются.",
            "Кандидаты CARRA2 не являются независимой проверкой ERA5 из-за общих граничных условий; спутниковая морфология, steering flow, split/merge и полный QC IMPMCT не входят в этот процесс.",
            "Результат не является независимой метеорологической валидацией или прогнозом.",
        ]
        # A report is a publishable product only when provenance and time span
        # are evidenced by the plan or processed frames. Missing data yields an
        # explicit no-product report below; source/time metadata is never guessed.
        passport = product_passport(
            product_id="arktika.pmc.archive_report",
            algorithm_id=ALGORITHM_ID,
            algorithm_version=ALGORITHM_VERSION,
            maturity="experimental",
            quantity_kind="candidate_track_catalog",
            quantity="Каталог наблюдавшихся модельных кандидатов и сегментов треков",
            units="tracks",
            valid_period=period,
            data_kind="analysis",
            grid=target_grid,
            source_grids=source_grids,
            nodata="Отсутствующие и ошибочные сроки неизвестны и исключены из связности треков",
            quality_control={"status": "partial" if incomplete else "processed",
                              "processed_frames": len(successful),
                              "failed_frames": len(errors),
                              "track_count": sum(item["track_count"] for item in track_rows)},
            sources=source_refs,
            calibration_version="not_applicable",
            calibration_status="not_applicable",
            limitations=report_limitations,
            uncertainty={"status": "not_assessed", "method": None},
        ) if successful and source_refs and source_grids and period else None
    else:
        report_limitations = [
            "Отсутствующие, неполные и ошибочные сроки не трактуются как отсутствие ПМЦ.",
            "Треки строятся только внутри непрерывных последовательностей наблюдавшихся модельных кандидатов.",
            "Журнал появления описывает только первый вывод модельного экрана и предшествующие кадры; физическое время и место genesis не оцениваются.",
            "Кандидаты CARRA2 не являются независимой проверкой ERA5 из-за общих граничных условий; спутниковая морфология, steering flow, split/merge и полный QC IMPMCT не входят в этот процесс.",
            "Результат не является независимой метеорологической валидацией или прогнозом.",
        ]
    report = {
        "task_id": task_id,
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "maturity": "experimental",
        "output_status": "candidate_only" if passport is not None else "not_publishable",
        "forecast": False,
        "status": "partial" if incomplete else "completed",
        "plan": plan,
        "coverage": coverage,
        "frames": frame_records,
        "tracks_by_source": track_rows,
        "cross_source_candidate_agreement": source_agreement or {
            "status": "not_applicable_single_source",
            "maturity": "experimental",
            "confirmation_status": "not_confirmed",
            "genesis_assessment": "not_provided",
        },
        "satellite_comparison": {
            "status": satellite_status,
            "comparison_basis": "пиксельное сравнение маски кандидатов и вручную импортированной спутниковой разметки",
            "validation_status": "not_meteorological_validation",
            "expected_frame_count": expected_pair_count,
            "matched_frame_count": observed_count,
            "unknown_frame_count": max(0, expected_pair_count - observed_count),
            "unknown_means": "нет точной пары наблюдение–срок–сетка; это не означает отсутствие ПМЦ",
            "metrics": satellite_metrics,
            "frames": satellite_frames,
        },
        "limitations": report_limitations,
    }
    if passport is not None:
        report["passport"] = passport
    else:
        report["publication"] = {
            "status": "not_publishable",
            "reason": "Нет успешно обработанных кадров с подтверждёнными источниками, временным периодом и сеткой.",
        }
    return report
