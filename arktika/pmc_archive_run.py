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
from .product_schema import product_passport


ALGORITHM_ID = "arktika.experimental_pmc_archive_run"
ALGORITHM_VERSION = "0.1.0"
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


def build_archive_report(*, task_id: str, plan: dict[str, Any],
                         expected_times: dict[str, list[str]],
                         interval_hours: dict[str, int],
                         frame_records: list[dict[str, Any]],
                         cached_fields: list[dict[str, Any]],
                         target_grid: dict[str, Any] | None = None) -> dict[str, Any]:
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
                               "unresolved_associations": []})
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
        track_rows.append({"source": source, "track_count": len(tracks), "tracks": tracks,
                           "unresolved_associations": associations,
                           "limitations": linked["limitations"] if segments else []})

    incomplete = any(item["unprocessed_steps"] or item["failed_steps"] for item in coverage)
    if target_grid is None:
        target_grid = next((row["result"].get("grid") for row in successful
                            if isinstance(row["result"].get("grid"), dict)), None)
    passport = None
    if isinstance(target_grid, dict):
        all_expected = [stamp for stamps in expected_times.values() for stamp in stamps]
        period_start = plan.get("start") or (min(all_expected) if all_expected else None)
        period_end = plan.get("end") or (max(all_expected) if all_expected else None)
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
            if not refs and isinstance(source, str) and source.strip():
                identity = (source.strip(), "")
                if identity not in seen_sources:
                    source_refs.append({"source_id": identity[0]})
                    seen_sources.add(identity)
            grids = field_passport.get("source_grids", []) if isinstance(field_passport, dict) else []
            if isinstance(grids, list):
                for source_grid in grids:
                    if isinstance(source_grid, dict):
                        key = repr(sorted(source_grid.items()))
                        if key not in seen_grids:
                            source_grids.append(source_grid)
                            seen_grids.add(key)
        if not source_grids:
            source_grids = [{"role": "detector_input_grid", **target_grid}]
        report_limitations = [
            "Отсутствующие, неполные и ошибочные сроки не трактуются как отсутствие ПМЦ.",
            "Треки строятся только внутри непрерывных последовательностей наблюдавшихся модельных кандидатов.",
            "Начало трека — первый наблюдавшийся срок данного сегмента; место и время зарождения не оцениваются.",
            "Кандидаты CARRA2 не являются независимой проверкой ERA5 из-за общих граничных условий; спутниковая морфология, steering flow, split/merge и полный QC IMPMCT не входят в этот процесс.",
            "Результат не является независимой метеорологической валидацией или прогнозом.",
        ]
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
        ) if source_refs and period else None
    else:
        report_limitations = [
            "Отсутствующие, неполные и ошибочные сроки не трактуются как отсутствие ПМЦ.",
            "Треки строятся только внутри непрерывных последовательностей наблюдавшихся модельных кандидатов.",
            "Начало трека — первый наблюдавшийся срок данного сегмента; место и время зарождения не оцениваются.",
            "Кандидаты CARRA2 не являются независимой проверкой ERA5 из-за общих граничных условий; спутниковая морфология, steering flow, split/merge и полный QC IMPMCT не входят в этот процесс.",
            "Результат не является независимой метеорологической валидацией или прогнозом.",
        ]
    report = {
        "task_id": task_id,
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "maturity": "experimental",
        "output_status": "candidate_only",
        "forecast": False,
        "status": "partial" if incomplete else "completed",
        "plan": plan,
        "coverage": coverage,
        "frames": frame_records,
        "tracks_by_source": track_rows,
        "satellite_comparison": {"status": "not_supplied", "metrics": None},
        "limitations": report_limitations,
    }
    if passport is not None:
        report["passport"] = passport
    return report
