"""Immutable metadata registry for the currently supported reanalysis sources.

This module describes source capabilities only. It performs no network access and
does not claim that a source, field, or derived product has been independently
validated. Provider-specific request and transport behavior remains in the
existing catalog and transport modules until callers are migrated deliberately.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol
from types import MappingProxyType

from .catalog import SOURCES, VARIABLES, supports as catalog_supports


class ReanalysisAdapter(Protocol):
    """Read-only metadata required to identify a reanalysis source adapter."""

    @property
    def source_id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def transport_id(self) -> str: ...

    @property
    def auth_provider(self) -> str: ...

    @property
    def dataset_ids(self) -> tuple[str, ...]: ...

    @property
    def supported_variables(self) -> frozenset[str]: ...


@dataclass(frozen=True, slots=True)
class AdapterMetadata:
    """Immutable source metadata; no credentials or live client are stored."""

    source_id: str
    name: str
    transport_id: str
    auth_provider: str
    dataset_ids: tuple[str, ...]
    supported_variables: frozenset[str]


def _supported_variables(source_id: str) -> frozenset[str]:
    return frozenset(
        key for key in VARIABLES if catalog_supports(source_id, key)
    )


_ADAPTERS: dict[str, AdapterMetadata] = {
    "era5": AdapterMetadata(
        source_id="era5",
        name=str(SOURCES["era5"]["name"]),
        transport_id="cds",
        auth_provider=str(SOURCES["era5"]["auth"]),
        dataset_ids=("reanalysis-era5-pressure-levels", "reanalysis-era5-single-levels"),
        supported_variables=_supported_variables("era5"),
    ),
    "carra2": AdapterMetadata(
        source_id="carra2",
        name=str(SOURCES["carra2"]["name"]),
        transport_id="cds",
        auth_provider=str(SOURCES["carra2"]["auth"]),
        dataset_ids=("reanalysis-pan-carra",),
        supported_variables=_supported_variables("carra2"),
    ),
    "merra2": AdapterMetadata(
        source_id="merra2",
        name=str(SOURCES["merra2"]["name"]),
        transport_id="earthdata_cmr_opendap",
        auth_provider=str(SOURCES["merra2"]["auth"]),
        dataset_ids=("M2I3NPASM", "M2I1NXASM"),
        supported_variables=_supported_variables("merra2"),
    ),
}

ADAPTERS: Mapping[str, AdapterMetadata] = MappingProxyType(_ADAPTERS)


def get_adapter(source_id: str) -> ReanalysisAdapter:
    """Return a registered source adapter or a safe, user-facing error."""
    if not isinstance(source_id, str) or source_id not in ADAPTERS:
        raise ValueError("Неизвестный источник реанализа.")
    return ADAPTERS[source_id]


def supports_variable(source_id: str, variable_id: str) -> bool:
    """Return False for unknown source/field IDs without raising catalog errors."""
    adapter = ADAPTERS.get(source_id) if isinstance(source_id, str) else None
    return bool(
        adapter is not None
        and isinstance(variable_id, str)
        and variable_id in adapter.supported_variables
    )


def require_supported(source_id: str, variable_id: str) -> ReanalysisAdapter:
    """Return the adapter only when it advertises the requested field."""
    adapter = get_adapter(source_id)
    if not isinstance(variable_id, str) or variable_id not in adapter.supported_variables:
        raise ValueError("Выбранное поле не поддерживается источником реанализа.")
    return adapter
