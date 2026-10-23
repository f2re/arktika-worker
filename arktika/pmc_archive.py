"""Validate and describe a retrospective PMC research plan.

Planning is deliberately separate from the candidate detector: this module
never downloads data, derives meteorological values, or asserts that a PMC was
observed. It reports which inputs are currently available and what remains.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .reanalysis.catalog import SOURCES, request_plan, validate_area

UTC = timezone.utc
SUPPORTED_SOURCES = ("era5", "carra2", "merra2")
ALGORITHM_ID = "arktika.experimental_pmc_archive_plan"
ALGORITHM_VERSION = "0.1.0"


def _utc_hour(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name}: укажите дату и время с часовым поясом UTC.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name}: неверная дата и время ISO 8601.") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name}: время должно иметь явный пояс UTC.")
    if parsed.minute or parsed.second or parsed.microsecond:
        raise ValueError(f"{name}: нужны точные целые часы UTC.")
    return parsed.astimezone(UTC)


def _times(start: datetime, end: datetime, source: str) -> list[datetime]:
    step = SOURCES[source]["step_hours"]
    first = start
    while first <= end and first.hour % step:
        first += timedelta(hours=1)
    result = []
    current = first
    while current <= end:
        result.append(current)
        current += timedelta(hours=step)
    return result


def build_plan(data: dict[str, Any], *, credentials: dict[str, bool], cached_fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a bounded metadata-only plan; no source requests are submitted."""
    sources = data.get("sources")
    if (not isinstance(sources, list) or not sources or len(sources) > 3
            or any(not isinstance(source, str) or source not in SUPPORTED_SOURCES for source in sources)
            or len(set(sources)) != len(sources)):
        raise ValueError("Выберите один или несколько источников из ERA5, CARRA2 и MERRA-2.")
    start = _utc_hour(data.get("start"), "Начало")
    end = _utc_hour(data.get("end"), "Конец")
    if end < start:
        raise ValueError("Конец интервала должен быть не раньше начала.")
    if end > datetime.now(UTC):
        raise ValueError("Архивное исследование принимает только уже наступившие сроки.")
    area = validate_area(data.get("area"))
    if "carra2" in sources and area[2] < 40:
        raise ValueError("CARRA2 не покрывает выбранную южную границу области.")

    source_rows = []
    expected = {}
    for source in sources:
        if start.year < SOURCES[source]["start"]:
            raise ValueError(f"{SOURCES[source]['name']}: начало выходит за архивный период источника.")
        dates = _times(start, end, source)
        if len(dates) > 250_000:
            raise ValueError("Интервал слишком велик для одного плана; разделите архив на периоды.")
        # Validate each source's time cadence, period and regional coverage.
        for stamp in dates:
            request_plan({"source": source, "variable": "wind", "time": stamp.isoformat(),
                          "level": 850, "area": area})
        expected[source] = dates
        cached = [field for field in cached_fields
                  if field.get("source") == source and field.get("variable") == "wind"
                  and field.get("level") == 850 and field.get("time")]
        cached_times = {str(field["time"]).replace("+00:00", "Z") for field in cached}
        matched = [stamp for stamp in dates if stamp.isoformat().replace("+00:00", "Z") in cached_times]
        source_rows.append({
            "id": source,
            "name": SOURCES[source]["name"],
            "interval_hours": SOURCES[source]["step_hours"],
            "first_year": SOURCES[source]["start"],
            "support_status": "wind_850_available",
            "credential_status": "ready" if credentials.get(source, False) else "required",
            "step_count": len(dates),
            "cached_step_count": len(matched),
            "missing_step_count": len(dates) - len(matched),
        })

    return {
        "maturity": "experimental",
        "output_status": "candidate_only",
        "forecast": False,
        "algorithm": {"id": ALGORITHM_ID, "version": ALGORITHM_VERSION},
        "plan": {"start": start.isoformat().replace("+00:00", "Z"),
                 "end": end.isoformat().replace("+00:00", "Z"), "area": area,
                 "sources": list(sources), "step_count": sum(len(expected[source]) for source in sources)},
        "sources": source_rows,
        "satellite": {"status": "manual_mask_required",
                      "note": "Нужны атрибутированная наблюдательная маска/разметка и её QC. RGB-цвета и спектральные рецепты не используются как подтверждение ПМЦ."},
        "missing_inputs": [
            "Планирование не запускает расчёт и не загружает данные. Для отдельного запуска нужны заранее сохранённые поля мгновенного ветра 850 гПа из ERA5 или MERRA-2; CARRA2 пока не поддержан архивным детектором.",
            "Нет подключения к архиву спутниковой разметки IMPMCT/AVHRR в приложении.",
            "Не реализованы steering flow, разделение пиков, слияние/распад, полный QC IMPMCT и оценка места зарождения.",
        ],
        "limitations": [
            "Этот план не является детектором, прогнозом или указанием места зарождения.",
            "Опубликованный IMPMCT относится к архиву Nordic Seas 2001–2024 и построен на ERA5; перенос параметров на CARRA2/MERRA-2 требует отдельной проверки.",
            "CARRA2 использует ERA5 в граничных условиях; их согласие не является независимым подтверждением.",
            "Неизвестные и непокрытые спутниковые сроки не считаются случаями без ПМЦ.",
        ],
}
