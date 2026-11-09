"""Refuse explicit artificial-data labels; absence of a label proves no origin."""
from __future__ import annotations

import re
from collections.abc import Mapping


_ARTIFICIAL_KINDS = {"synthetic", "synthetic_demo", "demo"}
_ARTIFICIAL_LABEL = re.compile(
    r"^\s*synthetic(?:\b|_)|\bsynthetic[ _-]+(?:numerical|pressure|software|test|demo|fixture|data|field|profile|wind)\b"
    r"|\bсинтетическ[а-яё]*\b", re.IGNORECASE)


def reject_explicit_synthetic_metadata(metadata):
    """Check only declared data kind and source/title, never numbers or filenames.

    This is a refusal gate, not authentication of a user-selected data source.
    In particular, processing history may legitimately mention a software test.
    """
    if not isinstance(metadata, Mapping):
        return
    declared = any(str(metadata.get(key, "")).strip().lower() in _ARTIFICIAL_KINDS
                   for key in ("data_kind", "profile_kind"))
    labelled = any(isinstance(metadata.get(key), str) and _ARTIFICIAL_LABEL.search(metadata[key])
                   for key in ("source", "title"))
    if declared or labelled:
        raise ValueError("Синтетические и демонстрационные данные не поддерживаются в рабочем анализе. Загрузите реальные данные наблюдений или модели.")
