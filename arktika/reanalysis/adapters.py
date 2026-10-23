"""Trusted local adapters for planning and preparing canonical reanalysis fields.

Adapters are Python implementations registered by application code. They are
never discovered from user files or loaded dynamically from the UI. Provider
credentials are transient method arguments and are not part of adapter state.
"""
from __future__ import annotations

import copy
import json
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.metadata import entry_points
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from .catalog import SOURCES, VARIABLES
from .catalog import supports as catalog_supports


class ReanalysisAdapter(Protocol):
    """Executable source contract; outputs use the shared canonical field schema."""

    source_id: str
    name: str
    transport_id: str
    auth_provider: str
    credential_request: Callable[[], dict[str, Any]]
    dataset_ids: tuple[str, ...]
    supported_variables: frozenset[str]

    def plan(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def fetch(self, plan: dict[str, Any], credential: dict[str, Any],
              folder: Path, progress: Callable[[str], None]) -> tuple[list[Any], list[dict[str, Any]]]: ...

    def normalize(self, dataset: Any, plan: dict[str, Any],
                  area: list[float] | None = None) -> Any: ...


@dataclass(frozen=True, slots=True)
class CredentialField:
    """Safe operator-facing input description; values are never adapter metadata."""

    id: str
    label: str
    input_type: str = "password"
    required: bool = True
    help: str = ""
    max_length: int = 4096


@dataclass(frozen=True, slots=True)
class AdapterMetadata:
    """Immutable descriptive metadata; it never contains a credential or client."""

    source_id: str
    name: str
    transport_id: str
    auth_provider: str
    dataset_ids: tuple[str, ...]
    supported_variables: frozenset[str]
    credential_fields: tuple[CredentialField, ...] = ()
    credential_format: str = "fields"
    credential_help: str = ""
    # Operator-declared endpoint metadata. It is not an egress control unless
    # the selected transport separately enforces it.
    allowed_hosts: tuple[str, ...] = ()
    adapter_version: str = "1.0.0"
    maturity: str = "implemented"


@dataclass(frozen=True, slots=True)
class AdapterImplementation:
    """A trusted code adapter with explicit planning, fetching, and normalization."""

    metadata: AdapterMetadata
    planner: Callable[[dict[str, Any]], dict[str, Any]]
    fetcher: Callable[[dict[str, Any], dict[str, Any], Path, Callable[[str], None]],
                      tuple[list[Any], list[dict[str, Any]]]]
    normalizer: Callable[[Any, dict[str, Any], list[float] | None], Any]
    catalogue_builder: Callable[[], dict[str, Any]] | None = None

    @property
    def source_id(self) -> str:
        return self.metadata.source_id

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def transport_id(self) -> str:
        return self.metadata.transport_id

    @property
    def auth_provider(self) -> str:
        return self.metadata.auth_provider

    @property
    def dataset_ids(self) -> tuple[str, ...]:
        return self.metadata.dataset_ids

    @property
    def supported_variables(self) -> frozenset[str]:
        return self.metadata.supported_variables

    @property
    def adapter_version(self) -> str:
        return self.metadata.adapter_version

    @property
    def maturity(self) -> str:
        return self.metadata.maturity

    def credential_request(self) -> dict[str, Any]:
        """Return sanitized form metadata, never credential values or adapter code."""
        return {
            "provider": self.auth_provider,
            "format": self.metadata.credential_format,
            "help": self.metadata.credential_help,
            "fields": [
                {"id": field.id, "label": field.label, "type": field.input_type,
                 "required": field.required, "help": field.help,
                 "max_length": field.max_length}
                for field in self.metadata.credential_fields
            ],
        }

    def catalogue_entry(self) -> dict[str, Any] | None:
        if self.catalogue_builder is None:
            return None
        entry=self.catalogue_builder()
        if not isinstance(entry,dict) or entry.get("id")!=self.source_id:
            raise ValueError("Каталог адаптера должен возвращать запись с собственным id.")
        variables=entry.get("variables")
        if not isinstance(variables,list):
            raise ValueError("Каталог адаптера должен объявлять список поддерживаемых полей.")
        ids=[item.get("id") for item in variables if isinstance(item,dict)]
        if len(ids)!=len(variables) or len(ids)!=len(set(ids)) or set(ids)-set(self.supported_variables):
            raise ValueError("Каталог адаптера содержит неподдерживаемые или повторные поля.")
        required = ("name", "auth", "start", "step_hours", "resolution_km")
        if any(key not in entry for key in required) or entry.get("auth") != self.auth_provider:
            raise ValueError("Каталог адаптера должен содержать имя, реквизиты, начало, шаг и разрешение.")
        if (not isinstance(entry["name"], str) or not entry["name"].strip()
                or isinstance(entry["start"], bool) or not isinstance(entry["start"], int)
                or isinstance(entry["step_hours"], bool) or not isinstance(entry["step_hours"], int)
                or entry["step_hours"] <= 0 or isinstance(entry["resolution_km"], bool)
                or not isinstance(entry["resolution_km"], (int, float))
                or not math.isfinite(entry["resolution_km"]) or entry["resolution_km"] <= 0):
            raise ValueError("Каталог адаптера содержит некорректные параметры источника.")
        for item in variables:
            if (not isinstance(item.get("title"), str) or not item["title"].strip()
                    or not isinstance(item.get("unit"), str) or not item["unit"].strip()
                    or item.get("vertical") not in ("pressure", "surface")
                    or not isinstance(item.get("levels"), list)
                    or not isinstance(item.get("styles"), list) or not item["styles"]):
                raise ValueError("Для каждого поля каталога обязательны название, единица, уровень и стили.")
        return copy.deepcopy(entry)

    def variable_spec(self, variable_id: str) -> dict[str, Any] | None:
        entry = self.catalogue_entry()
        if entry is not None:
            for item in entry["variables"]:
                if item["id"] == variable_id:
                    return item
        if variable_id in VARIABLES:
            from .catalog import LEVELS
            spec = VARIABLES[variable_id]
            return {"id": variable_id, "title": spec.title, "unit": spec.unit,
                    "vertical": spec.vertical,
                    "levels": list(LEVELS.get(self.source_id, [])) if spec.vertical == "pressure" else [],
                    "styles": ["fill", "arrows"] if variable_id.startswith("wind") else ["fill", "contours"]}
        return None

    def _validate_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(plan, dict) or plan.get("source") != self.source_id:
            raise ValueError("Адаптер вернул план для другого источника.")
        variable = plan.get("variable")
        if variable not in self.supported_variables:
            raise ValueError("План адаптера содержит неподдерживаемое поле.")
        try:
            stamp = datetime.fromisoformat(str(plan["time"]).replace("Z", "+00:00"))
            area = plan["area"]
            resolution_value = plan["resolution_km"]
            if (isinstance(resolution_value, bool) or not isinstance(resolution_value, (int, float))
                    or not math.isfinite(resolution_value) or resolution_value <= 0):
                raise ValueError
            resolution = float(resolution_value)
            if (stamp.tzinfo is None or stamp.utcoffset() != timedelta(0)
                    or stamp.minute or stamp.second or stamp.microsecond):
                raise ValueError
            if (not isinstance(area, list) or len(area) != 4
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in area)
                    or not all(map(math.isfinite, area))
                    or not -90 <= area[2] < area[0] <= 90
                    or not -180 <= area[1] <= 180 or not -180 <= area[3] <= 180
                    or area[1] == area[3] or resolution <= 0):
                raise ValueError
            if plan.get("time_kind") not in ("instantaneous_analysis", "analysis", "observation"):
                raise ValueError
            def reject_secrets(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        lowered=str(key).lower()
                        if (any(marker in lowered for marker in ("token", "password", "secret", "credential", "authorization", "api_key"))
                                or lowered.endswith(("_url", "_uri")) or lowered in ("url", "uri")):
                            raise ValueError
                        reject_secrets(child)
                elif isinstance(value, (list, tuple)):
                    for child in value: reject_secrets(child)
                elif isinstance(value, str):
                    if (re.search(r"[a-z][a-z0-9+.-]*://", value, re.IGNORECASE)
                            or re.search(r"(?i)(?:token|password|secret|api[_-]?key|authorization)\s*[:=]", value)):
                        raise ValueError
            reject_secrets(plan)
            json.dumps(plan, allow_nan=False)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("План адаптера должен содержать точный срок UTC, область, тип данных и разрешение.") from None
        spec = self.variable_spec(variable)
        if spec is None or not spec.get("unit"):
            raise ValueError("Каталог адаптера не описывает единицы выбранного поля.")
        level = plan.get("level")
        if spec["vertical"] == "pressure":
            if isinstance(level, bool) or not isinstance(level, (int, float)) or level not in spec["levels"]:
                raise ValueError("Уровень плана отсутствует в каталоге адаптера.")
        elif level is not None:
            raise ValueError("Поверхностное поле не должно иметь изобарический уровень.")
        return plan

    def plan(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.maturity == "planned":
            raise ValueError("Адаптер запланирован, но ещё не выполняет запросы.")
        plan = self.planner(request)
        return self._validate_plan(plan)

    def fetch(self, plan: dict[str, Any], credential: dict[str, Any],
              folder: Path, progress: Callable[[str], None]):
        if not isinstance(plan, dict) or plan.get("source") != self.source_id:
            raise ValueError("План загрузки не совпадает с адаптером источника.")
        self._validate_plan(plan)
        if not isinstance(credential, dict):
            raise ValueError("Данные доступа источника имеют неверный формат.")
        datasets, provenance = self.fetcher(plan, credential, folder, progress)
        if not isinstance(datasets, list) or not datasets:
            raise ValueError("Адаптер не вернул наборы данных для нормализации.")
        if not isinstance(provenance, list):
            raise ValueError("Происхождение результата должно быть списком записей.")
        return [self.normalize(dataset, plan, plan["area"]) for dataset in datasets], provenance

    def normalize(self, dataset: Any, plan: dict[str, Any],
                  area: list[float] | None = None):
        if not isinstance(plan, dict) or plan.get("source") != self.source_id:
            raise ValueError("План нормализации не совпадает с адаптером источника.")
        self._validate_plan(plan)
        result = self.normalizer(dataset, plan, area)
        return self.validate_canonical(result, plan)

    def validate_canonical(self, result: Any, plan: dict[str, Any]):
        self._validate_plan(plan)
        attrs = getattr(result, "attrs", {})
        expected = {"source": self.source_id, "variable": plan["variable"], "time": plan["time"],
                    "time_kind": plan["time_kind"], "resolution_km": float(plan["resolution_km"]),
                    "geolocation_crs": "EPSG:4326"}
        if any(attrs.get(key) != value for key, value in expected.items()):
            raise ValueError("Нормализованное поле не совпадает с источником, полем или сроком плана.")
        spec = self.variable_spec(plan["variable"])
        if attrs.get("unit") != spec["unit"]:
            raise ValueError("Единицы нормализованного поля не совпадают с каталогом адаптера.")
        try:
            if json.loads(attrs.get("level", "null")) != plan.get("level"):
                raise ValueError
            if json.loads(attrs.get("area", "null")) != plan["area"]:
                raise ValueError
            values = result["value"]
            lat, lon = result["latitude"], result["longitude"]
            if values.ndim != 2 or lat.shape != values.shape or lon.shape != values.shape:
                raise ValueError
            import numpy as np
            lat_values=np.asarray(lat.values,dtype=float);lon_values=np.asarray(lon.values,dtype=float)
            valid_geo=np.isfinite(lat_values)&np.isfinite(lon_values)
            if (not valid_geo.any() or np.any((lat_values[valid_geo]<-90)|(lat_values[valid_geo]>90))
                    or np.any((lon_values[valid_geo]<-180)|(lon_values[valid_geo]>180))
                    or attrs.get("geolocation_crs") != "EPSG:4326"):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("Нормализованное поле должно содержать согласованные значения и 2-D координаты.") from None
        return result


class AdapterRegistry:
    """Mutable only through explicit trusted-code registration."""

    def __init__(self, adapters: tuple[AdapterImplementation, ...] = ()):
        self._items: dict[str, AdapterImplementation] = {}
        self._lock = threading.RLock()
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: AdapterImplementation, *, replace: bool = False) -> None:
        if not isinstance(adapter, AdapterImplementation):
            raise TypeError("Реестр принимает только проверенные реализации DataAdapter.")
        meta = adapter.metadata
        if (not isinstance(meta.source_id, str) or not meta.source_id.strip()
                or not callable(adapter.planner) or not callable(adapter.fetcher)
                or not callable(adapter.normalizer)):
            raise ValueError("Адаптер должен иметь source_id и методы plan/fetch/normalize.")
        if not isinstance(meta.auth_provider, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", meta.auth_provider):
            raise ValueError("Идентификатор провайдера доступа должен быть безопасным slug.")
        if meta.auth_provider == "none" and meta.credential_fields:
            raise ValueError("Локальный провайдер none не может запрашивать реквизиты.")
        if meta.credential_format != "fields" or not isinstance(meta.credential_fields, tuple):
            raise ValueError("Формат реквизитов адаптера должен быть fields с неизменяемой схемой.")
        field_ids = set()
        for field in meta.credential_fields:
            if (not isinstance(field, CredentialField)
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", field.id)
                    or field.id in field_ids
                    or not isinstance(field.label, str) or not field.label.strip() or len(field.label) > 100
                    or field.input_type not in ("text", "password")
                    or not isinstance(field.required, bool)
                    or not isinstance(field.help, str) or len(field.help) > 300
                    or isinstance(field.max_length, bool) or not isinstance(field.max_length, int)
                    or not 1 <= field.max_length <= 8192):
                raise ValueError("Схема реквизитов адаптера содержит небезопасное или некорректное поле.")
            field_ids.add(field.id)
        if (not isinstance(meta.credential_help, str) or len(meta.credential_help) > 500
                or not isinstance(meta.allowed_hosts, tuple)
                or any(not isinstance(host, str) or not re.fullmatch(
                    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))+",
                    host.lower()) for host in meta.allowed_hosts)
                or len(set(h.lower() for h in meta.allowed_hosts)) != len(meta.allowed_hosts)):
            raise ValueError("Справка или список узлов доступа адаптера некорректны.")
        if meta.auth_provider != "none" and not meta.credential_fields:
            raise ValueError("Провайдер доступа должен явно описать поля для ввода реквизитов.")
        if meta.maturity not in ("implemented", "experimental", "planned"):
            raise ValueError("Для адаптера нужно явно указать статус готовности.")
        if meta.source_id not in SOURCES and adapter.catalogue_builder is None:
            raise ValueError("Новый источник должен предоставить пользовательскую запись каталога.")
        if meta.source_id in SOURCES and meta.source_id in self._items:
            raise ValueError("Встроенные адаптеры нельзя заменять через публичную регистрацию.")
        with self._lock:
            if meta.source_id in self._items and not replace:
                raise ValueError("Адаптер источника уже зарегистрирован.")
            self._items[meta.source_id] = adapter

    def get(self, source_id: str) -> AdapterImplementation:
        with self._lock:
            if not isinstance(source_id, str) or source_id not in self._items:
                raise ValueError("Неизвестный источник реанализа.")
            return self._items[source_id]

    def unregister(self, source_id: str) -> AdapterImplementation:
        if source_id in SOURCES:
            raise ValueError("Встроенные адаптеры нельзя удалять.")
        if not isinstance(source_id,str) or source_id not in self._items:
            raise ValueError("Адаптер для удаления не найден.")
        with self._lock:
            return self._items.pop(source_id)

    def snapshot(self) -> Mapping[str, AdapterImplementation]:
        with self._lock:
            return MappingProxyType(dict(self._items))

    def supports(self, source_id: str, variable_id: str) -> bool:
        with self._lock:
            adapter = self._items.get(source_id) if isinstance(source_id, str) else None
        return bool(adapter is not None and isinstance(variable_id, str)
                    and variable_id in adapter.supported_variables)


def _builtin_supported_variables(source_id: str) -> frozenset[str]:
    return frozenset(key for key in VARIABLES if catalog_supports(source_id, key))


def _builtin_planner(source_id: str):
    def plan(request: dict[str, Any]) -> dict[str, Any]:
        from .catalog import _build_request_plan
        if not isinstance(request, dict) or request.get("source") != source_id:
            raise ValueError("План запроса не совпадает с адаптером источника.")
        return _build_request_plan(request, expected_source=source_id)
    return plan


def _builtin_fetcher(transport_id: str):
    def fetch(plan, credential, folder, progress):
        from .transport import fetch_cds, fetch_merra
        function = {"cds": fetch_cds, "earthdata_cmr_opendap": fetch_merra}.get(transport_id)
        if function is None:
            raise ValueError("Для источника пока не подключён транспорт данных.")
        # Transport returns loaded source datasets; the adapter applies its one canonical normalizer.
        return function(plan, credential, folder, progress, normalizer=lambda dataset, _plan, _area=None: dataset)
    return fetch


def _builtin_normalizer(dataset, plan, area=None):
    from .normalize import normalize
    return normalize(dataset, plan, area)


def _builtin_adapter(source_id: str, dataset_ids: tuple[str, ...]) -> AdapterImplementation:
    source = SOURCES[source_id]
    transport_id = "cds" if source_id in ("era5", "carra2") else "earthdata_cmr_opendap"
    return AdapterImplementation(
        metadata=AdapterMetadata(
            source_id=source_id,
            name=str(source["name"]),
            transport_id=transport_id,
            auth_provider=str(source["auth"]),
            dataset_ids=dataset_ids,
            supported_variables=_builtin_supported_variables(source_id),
            credential_fields=(
                (CredentialField("token", "Токен CDS", "password", max_length=8192),)
                if source_id in ("era5", "carra2") else
                (CredentialField("token", "Токен Earthdata или пароль .netrc", "password", max_length=8192),)
            ),
            credential_help=("Доступ CDS используется ERA5 и CARRA2." if source_id in ("era5", "carra2")
                             else "Для MERRA-2 используется Earthdata."),
            allowed_hosts=(("cds.climate.copernicus.eu",) if source_id in ("era5", "carra2")
                           else ("urs.earthdata.nasa.gov", "goldsmr4.gesdisc.eosdis.nasa.gov")),
        ),
        planner=_builtin_planner(source_id),
        fetcher=_builtin_fetcher(transport_id),
        normalizer=_builtin_normalizer,
    )


DEFAULT_REGISTRY = AdapterRegistry((
    _builtin_adapter("era5", ("reanalysis-era5-pressure-levels", "reanalysis-era5-single-levels")),
    _builtin_adapter("carra2", ("reanalysis-pan-carra",)),
    _builtin_adapter("merra2", ("M2I3NPASM", "M2I1NXASM")),
))
_METADATA: dict[str, AdapterMetadata] = {
    source: implementation.metadata
    for source, implementation in DEFAULT_REGISTRY.snapshot().items()
}
ADAPTERS: Mapping[str, AdapterMetadata] = MappingProxyType(_METADATA)
_LOADED_ENTRYPOINTS: set[tuple[int, str]] = set()
_ENTRYPOINT_LOCK = threading.RLock()


def get_runtime_adapter(source_id: str, *, registry: AdapterRegistry = DEFAULT_REGISTRY) -> ReanalysisAdapter:
    load_installed_adapters(registry=registry)
    return registry.get(source_id)


def register_adapter(adapter: AdapterImplementation, *, replace: bool = False,
                     registry: AdapterRegistry = DEFAULT_REGISTRY) -> None:
    """Register a trusted, code-defined adapter; user files are never imported."""
    registry.register(adapter, replace=replace)
    if registry is DEFAULT_REGISTRY:
        _METADATA[adapter.source_id] = adapter.metadata


def unregister_adapter(source_id: str, *, registry: AdapterRegistry = DEFAULT_REGISTRY) -> None:
    """Remove a trusted adapter, primarily for controlled tests and reloads."""
    registry.unregister(source_id)
    if registry is DEFAULT_REGISTRY:
        _METADATA.pop(source_id,None)


def load_installed_adapters(*, registry: AdapterRegistry = DEFAULT_REGISTRY) -> None:
    """Load code installed by the operator through the documented entry-point group."""
    with _ENTRYPOINT_LOCK:
        try:
            points = entry_points(group="arktika.reanalysis_adapters")
        except TypeError:  # Python 3.9 compatibility
            points = entry_points().get("arktika.reanalysis_adapters", ())
        for point in sorted(points, key=lambda item: item.name):
            marker = (id(registry), point.name)
            if marker in _LOADED_ENTRYPOINTS:
                continue
            loaded = point.load()
            implementation = loaded() if callable(loaded) and not isinstance(loaded, AdapterImplementation) else loaded
            if not isinstance(implementation, AdapterImplementation):
                raise TypeError("Entry point адаптера должен возвращать AdapterImplementation.")
            register_adapter(implementation, registry=registry)
            _LOADED_ENTRYPOINTS.add(marker)


def get_adapter(source_id: str) -> AdapterMetadata:
    """Return descriptive metadata for compatibility with existing callers."""
    return get_runtime_adapter(source_id).metadata


def supports_variable(source_id: str, variable_id: str) -> bool:
    return DEFAULT_REGISTRY.supports(source_id, variable_id)


def require_supported(source_id: str, variable_id: str) -> ReanalysisAdapter:
    adapter = get_runtime_adapter(source_id)
    if not isinstance(variable_id, str) or variable_id not in adapter.supported_variables:
        raise ValueError("Выбранное поле не поддерживается источником реанализа.")
    return adapter
