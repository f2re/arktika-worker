"""Общий паспорт сохранённого продукта и его минимальная проверка."""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any

SCHEMA_VERSION = "1.0"
MATURITIES = {"implemented", "experimental", "planned"}
DATA_KINDS = {"observation", "analysis", "forecast", "synthetic"}
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_GRID_TYPE = re.compile(r"^(regular|curvilinear)(_[a-z0-9]+)*$")


def _validate_grid(value: Any, field: str) -> None:
    """Require declared geometry, while allowing hash-attested 2-D geolocation grids."""
    if (not isinstance(value, dict) or not isinstance(value.get("crs"), str)
            or not value["crs"].strip()
            or any(isinstance(value.get(key), bool) or not isinstance(value.get(key), int)
                   or value[key] <= 0 for key in ("width", "height"))):
        raise ValueError("Паспорт продукта: {} требует CRS, ширину и высоту сетки.".format(field))
    transform = value.get("transform")
    if transform is not None:
        if (not isinstance(transform, (list, tuple)) or len(transform) != 6
                or any(isinstance(item, bool) or not isinstance(item, (int, float))
                       or not math.isfinite(float(item)) for item in transform)):
            raise ValueError("Паспорт продукта: {} содержит неверную affine-привязку.".format(field))
        a, b, _, d, e, _ = map(float, transform)
        if abs(a * e - b * d) <= 1e-18:
            raise ValueError("Паспорт продукта: {} имеет вырожденную affine-сетку.".format(field))
        return
    grid_type = value.get("grid_type")
    digest = value.get("coordinate_sha256", value.get("geolocation_sha256"))
    geolocation = value.get("geolocation")
    if (not isinstance(grid_type, str) or not _GRID_TYPE.fullmatch(grid_type)
            or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
            or not (geolocation or value.get("coordinate_variables"))):
        raise ValueError(
            "Паспорт продукта: {} должен содержать affine-привязку либо тип и SHA-256 геолокации.".format(field)
        )


def product_passport(
    *,
    product_id: str,
    algorithm_id: str,
    algorithm_version: str,
    maturity: str,
    quantity_kind: str,
    quantity: str,
    units: str,
    valid_time: str | None = None,
    valid_period: dict[str, str] | None = None,
    data_kind: str,
    grid: dict[str, Any],
    source_grids: list[dict[str, Any]],
    nodata: str,
    quality_control: dict[str, Any],
    sources: list[dict[str, Any]],
    calibration_version: str,
    calibration_status: str,
    limitations: list[str],
    uncertainty: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Создаёт паспорт с обязательными полями; отсутствие оценки явно фиксируется."""
    if maturity not in MATURITIES:
        raise ValueError("Неизвестный статус готовности продукта.")
    if data_kind not in DATA_KINDS:
        raise ValueError("Неизвестный вид данных продукта.")
    for name, value in (
        ("product_id", product_id), ("algorithm_id", algorithm_id),
        ("algorithm_version", algorithm_version), ("quantity_kind", quantity_kind),
        ("quantity", quantity), ("units", units),
        ("nodata", nodata), ("calibration_version", calibration_version),
        ("calibration_status", calibration_status),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Паспорт продукта: обязательное поле {}.".format(name))
    _validate_grid(grid, "расчётной сетке")
    if (not isinstance(source_grids, list) or not source_grids
            or any(not isinstance(item, dict) for item in source_grids)):
        raise ValueError("Паспорт продукта: нужны сведения об исходной сетке.")
    for index, source_grid in enumerate(source_grids):
        _validate_grid(source_grid, "исходной сетке #{}".format(index + 1))
    if (valid_time is None) == (valid_period is None):
        raise ValueError("Паспорт продукта: укажите valid_time или valid_period.")
    if valid_time is not None:
        _validate_utc(valid_time, "valid_time")
    else:
        if (not isinstance(valid_period, dict)
                or set(valid_period) != {"start", "end"}):
            raise ValueError("Паспорт продукта: valid_period должен содержать start и end.")
        start = _validate_utc(valid_period["start"], "valid_period.start")
        end = _validate_utc(valid_period["end"], "valid_period.end")
        if end < start:
            raise ValueError("Паспорт продукта: valid_period.end раньше начала периода.")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Паспорт продукта: укажите хотя бы один источник.")
    if not isinstance(quality_control, dict):
        raise ValueError("Паспорт продукта: QC должен быть объектом.")
    if not isinstance(limitations, list) or any(not isinstance(item, str) for item in limitations):
        raise ValueError("Паспорт продукта: ограничения должны быть списком пояснений.")
    passport = {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "algorithm": {"id": algorithm_id, "version": algorithm_version},
        "maturity": maturity,
        "quantity_kind": quantity_kind,
        "quantity": quantity,
        "units": units,
        "data_kind": data_kind,
        "grid": grid,
        "source_grids": source_grids,
        "nodata": nodata,
        "quality_control": quality_control,
        "sources": sources,
        "calibration": {"version": calibration_version, "status": calibration_status},
        "limitations": limitations,
        "uncertainty": uncertainty or {"status": "not_assessed", "method": None},
    }
    if valid_time is not None:
        passport["valid_time"] = valid_time
    else:
        passport["valid_period"] = dict(valid_period)
    return passport


def _validate_utc(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError("Паспорт продукта: {} должен быть ISO 8601.".format(field))
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Паспорт продукта: {} должен быть ISO 8601.".format(field)) from None
    if stamp.tzinfo is None or stamp.utcoffset() != dt.timedelta(0):
        raise ValueError("Паспорт продукта: {} должен быть указан в UTC.".format(field))
    return stamp.astimezone(dt.timezone.utc)
