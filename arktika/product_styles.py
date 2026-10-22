"""Профили экранной растяжки для численных продуктов.

Стили привязаны к конкретному экземпляру продукта и версии алгоритма. Они
описывают только отображение значений ``values.tif`` и не меняют научные
данные, легенду исходного расчёта или готовые RGB-композиции.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
_CONTINUOUS = {"absolute_temperature", "temperature_difference", "continuous", "stored_value"}


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("{} должен быть числом.".format(label))
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("{} должен быть конечным числом.".format(label))
    return result


def _palette(value: Any) -> list[list[int]]:
    if not isinstance(value, list) or len(value) < 2:
        raise ValueError("Палитра должна содержать не менее двух цветовых точек.")
    result = []
    for stop in value:
        if (not isinstance(stop, (list, tuple)) or len(stop) != 3
                or any(isinstance(channel, bool) or not isinstance(channel, int)
                       or not 0 <= channel <= 255 for channel in stop)):
            raise ValueError("Каждая точка палитры должна быть RGB из целых чисел 0–255.")
        result.append(list(stop))
    return result


def _product_identity(product: dict[str, Any]) -> tuple[str, str]:
    passport = product.get("passport")
    algorithm = passport.get("algorithm") if isinstance(passport, dict) else None
    product_id = product.get("id")
    version = algorithm.get("version") if isinstance(algorithm, dict) else None
    if not isinstance(product_id, str) or not product_id.strip():
        raise ValueError("У продукта отсутствует идентификатор.")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("В паспорте продукта отсутствует версия алгоритма.")
    return product_id, version


def validate_styleable_product(product: dict[str, Any]) -> tuple[str, str]:
    """Возвращает ключ профиля только для непрерывного продукта с числовым растром."""
    if not isinstance(product, dict):
        raise ValueError("Нужны метаданные продукта.")
    if product.get("product") == "archive_rgb" or product.get("quantity_kind") == "rgb":
        raise ValueError("Цвета готовой RGB-композиции поставщика не перенастраиваются.")
    legend = product.get("legend")
    if not isinstance(legend, dict):
        raise ValueError("У продукта нет легенды с настройками отображения.")
    kind = legend.get("quantity_kind")
    if kind not in _CONTINUOUS:
        raise ValueError("Настройка шкалы доступна только для непрерывных численных продуктов.")
    files = product.get("files", [])
    if not isinstance(files, list) or "values.tif" not in files:
        raise ValueError("Для перенастройки шкалы нужен сохранённый числовой файл values.tif.")
    if not isinstance(legend.get("palette"), list):
        raise ValueError("В легенде продукта нет исходной палитры.")
    _palette(legend["palette"])
    _finite_number(legend.get("display_min"), "Исходный минимум")
    _finite_number(legend.get("display_max"), "Исходный максимум")
    if legend["display_min"] >= legend["display_max"]:
        raise ValueError("Исходный диапазон отображения должен иметь положительную ширину.")
    return _product_identity(product)


def default_profile(product: dict[str, Any]) -> dict[str, Any]:
    """Создаёт профиль только из экранных параметров легенды продукта."""
    product_id, algorithm_version = validate_styleable_product(product)
    legend = product["legend"]
    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": product_id,
        "algorithm_version": algorithm_version,
        "palette_id": "product-default",
        "palette": _palette(legend["palette"]),
        "minimum": _finite_number(legend["display_min"], "Минимум"),
        "maximum": _finite_number(legend["display_max"], "Максимум"),
        "scale_mode": "linear",
    }


def validate_profile(product: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Проверяет ввод профиля и возвращает нормализованный независимый объект."""
    expected = default_profile(product)
    if not isinstance(profile, dict):
        raise ValueError("Профиль цветовой шкалы должен быть объектом.")
    if profile.get("product_id") != expected["product_id"]:
        raise ValueError("Профиль относится к другому продукту.")
    if profile.get("algorithm_version") != expected["algorithm_version"]:
        raise ValueError("Профиль относится к другой версии алгоритма.")
    minimum = _finite_number(profile.get("minimum"), "Минимум")
    maximum = _finite_number(profile.get("maximum"), "Максимум")
    if minimum >= maximum:
        raise ValueError("Минимум должен быть меньше максимума.")
    scale_mode = profile.get("scale_mode", "linear")
    if scale_mode != "linear":
        raise ValueError("Поддерживается только линейная экранная растяжка.")
    palette = _palette(profile.get("palette"))
    palette_id = profile.get("palette_id", "custom")
    if not isinstance(palette_id, str) or not palette_id.strip() or len(palette_id) > 64:
        raise ValueError("Некорректный идентификатор палитры.")
    return {**expected, "palette_id": palette_id, "palette": palette,
            "minimum": minimum, "maximum": maximum, "scale_mode": scale_mode}


class ProductStyleStore:
    """Атомарное файловое хранилище отдельных профилей экранного отображения."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)

    @staticmethod
    def _key(product_id: str, algorithm_version: str) -> str:
        raw = (product_id + "\0" + algorithm_version).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _path(self, product_id: str, algorithm_version: str) -> Path:
        return self.root / (self._key(product_id, algorithm_version) + ".json")

    def get(self, product: dict[str, Any]) -> dict[str, Any]:
        profile = default_profile(product)
        path = self._path(profile["product_id"], profile["algorithm_version"])
        if not path.exists():
            return profile
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ValueError("Не удалось прочитать сохранённый профиль цветовой шкалы.") from None
        return validate_profile(product, saved)

    def save(self, product: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_profile(product, profile)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(normalized["product_id"], normalized["algorithm_version"])
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(normalized, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.replace(temporary, path)
        return normalized
