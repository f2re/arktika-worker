"""Доверенный локальный реестр адаптеров производных продуктов.

Адаптеры регистрируются кодом приложения. API/UI передают только идентификатор
адаптера и проверенные данные: модуль не импортирует код по пользовательскому
пути и не отвечает за получение реквизитов или сетевой транспорт.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol
import re

import numpy as np

from .product_schema import product_passport

_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_GRID_KEYS = ("crs", "width", "height", "transform")


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    """Публичное описание метода; полностью версионируется и не содержит кода."""

    adapter_id: str
    version: str
    product_id: str
    title: str
    algorithm_id: str
    maturity: str
    quantity_kind: str
    quantity: str
    units: str
    input_ids: tuple[str, ...]
    data_kind: str = "analysis"
    nodata: str = "NaN: нет данных"
    limitations: tuple[str, ...] = ()

    def validate(self) -> None:
        if not _ID.fullmatch(self.adapter_id) or not _ID.fullmatch(self.product_id):
            raise ValueError("Идентификатор адаптера или продукта имеет неверный формат.")
        if not _VERSION.fullmatch(self.version):
            raise ValueError("Версия адаптера должна иметь формат major.minor.patch.")
        for value in (self.title, self.algorithm_id, self.quantity_kind,
                      self.quantity, self.units, self.nodata):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Описание адаптера содержит пустое обязательное поле.")
        if (not isinstance(self.input_ids, tuple) or not self.input_ids
                or len(set(self.input_ids)) != len(self.input_ids)
                or any(not _ID.fullmatch(item) for item in self.input_ids)):
            raise ValueError("Адаптер должен объявить уникальные идентификаторы входов.")
        if self.maturity not in ("implemented", "experimental", "planned"):
            raise ValueError("Неизвестный статус готовности адаптера.")
        if self.data_kind not in ("observation", "analysis", "forecast", "synthetic"):
            raise ValueError("Неизвестный вид входных данных адаптера.")
        if not isinstance(self.limitations, tuple) or any(not isinstance(x, str) for x in self.limitations):
            raise ValueError("Ограничения адаптера должны быть текстовым списком.")


@dataclass(frozen=True, slots=True)
class SourceData:
    """Одно проверенное поле и его происхождение, без реквизитов транспорта."""

    input_id: str
    values: np.ndarray
    quantity: str
    units: str
    valid_time: str
    grid: Mapping[str, Any]
    source: Mapping[str, Any]
    data_kind: str = "analysis"


@dataclass(frozen=True, slots=True)
class DerivedProduct:
    """Результат адаптера с неизменяемым описанием метода и паспортом."""

    spec: AdapterSpec
    values: np.ndarray
    valid_time: str
    grid: Mapping[str, Any]
    sources: tuple[Mapping[str, Any], ...]
    passport: Mapping[str, Any]


class DerivedProductAdapter(Protocol):
    @property
    def spec(self) -> AdapterSpec: ...

    def derive(self, inputs: Mapping[str, SourceData]) -> np.ndarray: ...


def _utc(value: str) -> str:
    from datetime import datetime, timezone
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Для входного поля нужен срок ISO 8601 UTC.") from None
    if stamp.tzinfo is None or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
        raise ValueError("Для входного поля нужен срок UTC.")
    return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _grid_signature(grid: Mapping[str, Any]) -> tuple[Any, ...]:
    if not isinstance(grid, Mapping) or any(key not in grid for key in ("crs", "width", "height")):
        raise ValueError("Сетка входного поля должна содержать CRS и размеры.")
    width, height = grid["width"], grid["height"]
    if (not isinstance(grid["crs"], str) or not grid["crs"].strip()
            or isinstance(width, bool) or not isinstance(width, int) or width <= 0
            or isinstance(height, bool) or not isinstance(height, int) or height <= 0
            ):
        raise ValueError("Геометрия сетки входного поля некорректна.")
    if "transform" not in grid:
        grid_type = grid.get("grid_type")
        digest = grid.get("coordinate_sha256", grid.get("geolocation_sha256"))
        if (not isinstance(grid_type, str) or not _ID.fullmatch(grid_type.replace("-", "_"))
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not (grid.get("geolocation") or grid.get("coordinate_variables"))):
            raise ValueError("Сетка входного поля требует affine-привязку или контрольную сумму геолокации.")
        return (grid["crs"], width, height, grid_type, digest)
    if not isinstance(grid["transform"], (tuple, list)) or len(grid["transform"]) != 6:
        raise ValueError("Геометрия сетки входного поля некорректна.")
    transform = tuple(float(value) for value in grid["transform"])
    if not np.isfinite(transform).all():
        raise ValueError("Преобразование сетки должно быть конечным.")
    return (grid["crs"], width, height, transform)


def _source_record(source: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        raise ValueError("Для входного поля нужна запись источника.")
    # Accept only provenance identifiers and a content digest. Never persist auth,
    # local paths or request URLs in a derived-product passport.
    allowed = {"id", "dataset", "field_id", "sha256", "label", "level_hpa"}
    forbidden = {"credential", "token", "password", "login", "path", "uri", "url"}
    if any(key.lower() in forbidden for key in source):
        raise ValueError("Реквизиты, пути и URI не являются provenance продукта.")
    clean = {key: source[key] for key in allowed if key in source}
    if not clean.get("id") and not clean.get("dataset"):
        raise ValueError("В provenance нужно указать id или dataset источника.")
    return clean


class AdapterRegistry:
    """Реестр кода, доверенного локальному приложению; не является plugin loader."""

    def __init__(self) -> None:
        self._items: dict[str, DerivedProductAdapter] = {}

    def register(self, adapter: DerivedProductAdapter) -> None:
        if not callable(getattr(adapter, "derive", None)):
            raise ValueError("Локальный адаптер должен реализовать derive().")
        spec = getattr(adapter, "spec", None)
        if not isinstance(spec, AdapterSpec):
            raise ValueError("Локальный адаптер должен объявить AdapterSpec.")
        spec.validate()
        if spec.adapter_id in self._items:
            raise ValueError("Идентификатор адаптера уже зарегистрирован.")
        self._items[spec.adapter_id] = adapter

    def get(self, adapter_id: str) -> DerivedProductAdapter:
        if not isinstance(adapter_id, str) or adapter_id not in self._items:
            raise ValueError("Неизвестный адаптер производного продукта.")
        return self._items[adapter_id]

    def catalogue(self) -> tuple[dict[str, Any], ...]:
        """Возвращает только декларативные метаданные, не callable и не код."""
        return tuple({
            "id": a.spec.adapter_id,
            "version": a.spec.version,
            "product_id": a.spec.product_id,
            "title": a.spec.title,
            "algorithm_id": a.spec.algorithm_id,
            "maturity": a.spec.maturity,
            "quantity_kind": a.spec.quantity_kind,
            "quantity": a.spec.quantity,
            "units": a.spec.units,
            "inputs": a.spec.input_ids,
            "data_kind": a.spec.data_kind,
            "limitations": a.spec.limitations,
        } for a in self._items.values())

    def run(self, adapter_id: str, inputs: Mapping[str, SourceData]) -> DerivedProduct:
        adapter = self.get(adapter_id)
        spec = adapter.spec
        if not isinstance(inputs, Mapping) or set(inputs) != set(spec.input_ids):
            raise ValueError("Набор входных полей не совпадает с контрактом адаптера.")
        checked: dict[str, SourceData] = {}
        signature = None
        timestamp = None
        arrays = []
        sources = []
        for input_id in spec.input_ids:
            item = inputs[input_id]
            if not isinstance(item, SourceData) or item.input_id != input_id:
                raise ValueError("Идентификатор входного поля не совпадает с контрактом.")
            array = np.asarray(item.values)
            if array.ndim != 2 or array.dtype.kind not in "fiu":
                raise ValueError("Вход адаптера должен быть численным двумерным полем.")
            current_signature = _grid_signature(item.grid)
            if current_signature[1:3] != array.shape[::-1]:
                raise ValueError("Размер поля не совпадает с геометрией сетки.")
            current_time = _utc(item.valid_time)
            if signature is not None and current_signature != signature:
                raise ValueError("Входные поля должны иметь точно совпадающие сетки.")
            if timestamp is not None and current_time != timestamp:
                raise ValueError("Входные поля должны иметь один точный срок UTC.")
            signature, timestamp = current_signature, current_time
            arrays.append(array)
            sources.append(_source_record(item.source))
            checked[input_id] = item
        output = np.asarray(adapter.derive(MappingProxyType(checked)))
        if output.ndim != 2 or output.shape != arrays[0].shape or output.dtype.kind not in "fiu":
            raise ValueError("Адаптер вернул поле неправильной формы или типа.")
        output = np.array(output, dtype=np.float32, copy=True)
        output.setflags(write=False)
        grid = MappingProxyType(dict(checked[spec.input_ids[0]].grid))
        passport = product_passport(
            product_id=spec.product_id,
            algorithm_id=spec.algorithm_id,
            algorithm_version=spec.version,
            maturity=spec.maturity,
            quantity_kind=spec.quantity_kind,
            quantity=spec.quantity,
            units=spec.units,
            valid_time=timestamp,
            data_kind=spec.data_kind,
            grid=dict(grid),
            source_grids=[dict(grid)],
            nodata=spec.nodata,
            quality_control={"valid_pixels": int(np.isfinite(output).sum()),
                             "total_pixels": int(output.size)},
            sources=sources,
            calibration_version="not_applicable",
            calibration_status="source_units_verified_by_adapter_contract",
            limitations=list(spec.limitations),
        )
        return DerivedProduct(spec, output, timestamp, grid, tuple(MappingProxyType(s) for s in sources),
                              MappingProxyType(passport))


class WindSpeedAdapter:
    """Пример работающего адаптера: модуль восточного/северного ветра → скорость."""

    spec = AdapterSpec(
        adapter_id="wind_speed",
        version="1.0.0",
        product_id="wind_speed",
        title="Скорость ветра из компонентов",
        algorithm_id="arktika.wind_speed",
        maturity="implemented",
        quantity_kind="speed",
        quantity="Горизонтальная скорость ветра",
        units="m s-1",
        input_ids=("u", "v"),
        limitations=("Расчёт не проверяет точность исходного реанализа.",),
    )

    def derive(self, inputs: Mapping[str, SourceData]) -> np.ndarray:
        u, v = inputs["u"], inputs["v"]
        for item, expected in ((u, "eastward_wind"), (v, "northward_wind")):
            if item.quantity != expected or item.units != "m s-1":
                raise ValueError("Для скорости нужны eastward_wind/northward_wind в m s-1.")
        east, north = np.asarray(u.values, dtype=np.float64), np.asarray(v.values, dtype=np.float64)
        valid = np.isfinite(east) & np.isfinite(north)
        result = np.full(east.shape, np.nan, dtype=np.float32)
        result[valid] = np.hypot(east[valid], north[valid])
        return result


DEFAULT_ADAPTERS = AdapterRegistry()
DEFAULT_ADAPTERS.register(WindSpeedAdapter())
