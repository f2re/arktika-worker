"""Общий паспорт сохранённого продукта и его минимальная проверка."""
from __future__ import annotations

import datetime as dt
from typing import Any

SCHEMA_VERSION = "1.0"
MATURITIES = {"implemented", "experimental", "planned"}
DATA_KINDS = {"observation", "analysis", "forecast", "synthetic"}


def product_passport(
    *,
    product_id: str,
    algorithm_id: str,
    algorithm_version: str,
    maturity: str,
    quantity_kind: str,
    quantity: str,
    units: str,
    valid_time: str,
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
        ("quantity", quantity), ("units", units), ("valid_time", valid_time),
        ("nodata", nodata), ("calibration_version", calibration_version),
        ("calibration_status", calibration_status),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Паспорт продукта: обязательное поле {}.".format(name))
    if (not isinstance(grid, dict) or not grid.get("crs")
            or any(isinstance(grid.get(key), bool) or not isinstance(grid.get(key), int)
                   or grid[key] <= 0 for key in ("width", "height"))):
        raise ValueError("Паспорт продукта: сетке нужны CRS, ширина и высота.")
    if not isinstance(source_grids, list) or not source_grids:
        raise ValueError("Паспорт продукта: нужны сведения об исходной сетке.")
    try:
        stamp = dt.datetime.fromisoformat(valid_time.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Паспорт продукта: valid_time должен быть ISO 8601.") from None
    if stamp.tzinfo is None or stamp.utcoffset() != dt.timedelta(0):
        raise ValueError("Паспорт продукта: valid_time должен быть указан в UTC.")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Паспорт продукта: укажите хотя бы один источник.")
    if not isinstance(quality_control, dict):
        raise ValueError("Паспорт продукта: QC должен быть объектом.")
    if not isinstance(limitations, list) or any(not isinstance(item, str) for item in limitations):
        raise ValueError("Паспорт продукта: ограничения должны быть списком пояснений.")
    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "algorithm": {"id": algorithm_id, "version": algorithm_version},
        "maturity": maturity,
        "quantity_kind": quantity_kind,
        "quantity": quantity,
        "units": units,
        "data_kind": data_kind,
        "valid_time": valid_time,
        "grid": grid,
        "source_grids": source_grids,
        "nodata": nodata,
        "quality_control": quality_control,
        "sources": sources,
        "calibration": {"version": calibration_version, "status": calibration_status},
        "limitations": limitations,
        "uncertainty": uncertainty or {"status": "not_assessed", "method": None},
    }
