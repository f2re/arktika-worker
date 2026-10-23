"""Integrity checks for ordinary, locally saved satellite products."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .product_schema import validate_product_passport

_IDENTITY = re.compile(r"^[0-9a-f]{24}$")
_ARTIFACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def artifact_manifest(folder: Path, names: list[str]) -> dict[str, dict[str, Any]]:
    """Return SHA-256/size attestations for every non-metadata artifact."""
    manifest = {}
    for name in names:
        if name == "product.json":
            continue
        _safe_name(name)
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Артефакт продукта отсутствует или имеет небезопасный тип.")
        manifest[name] = {"sha256": _sha256(path), "size_bytes": path.stat().st_size}
    return manifest


def product_metadata_path(root: Path, identity: str) -> Path:
    """Resolve a product's metadata only when its directory entries are local."""
    if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity):
        raise ValueError("Неверный идентификатор продукта.")
    root = Path(root).resolve()
    folder = Path(root) / identity
    metadata = folder / "product.json"
    if (folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != root
            or metadata.is_symlink() or not metadata.is_file()
            or metadata.resolve().parent != folder.resolve()):
        raise ValueError("Каталог или паспорт сохранённого продукта небезопасен.")
    return metadata


def load_cached_product(root: Path, identity: str) -> dict[str, Any] | None:
    """Read a valid cache, or return None when the record predates manifests."""
    if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity):
        raise ValueError("Неверный идентификатор продукта.")
    root = Path(root).resolve()
    folder = root / identity
    if not folder.exists() and not folder.is_symlink():
        return None
    if folder.is_symlink() or folder.resolve().parent != root or not folder.is_dir():
        raise ValueError("Каталог кэша продукта небезопасен.")
    metadata = folder / "product.json"
    if metadata.is_symlink():
        raise ValueError("Паспорт кэша продукта небезопасен.")
    if not metadata.exists():
        return None
    record = json.loads(metadata.read_text(encoding="utf-8"))
    if isinstance(record, dict) and "artifacts" not in record:
        return None
    return validate_saved_product(folder, record)


def replace_product_cache(stage: Path, final: Path) -> None:
    """Replace only the canonical in-root cache directory after a full rebuild."""
    stage = Path(stage)
    final = Path(final)
    root = final.parent.resolve()
    if (not _IDENTITY.fullmatch(final.name) or final.is_symlink()
            or final.parent.resolve() != root or stage.parent.resolve() != root
            or stage.is_symlink() or not stage.is_dir()):
        raise ValueError("Небезопасный путь замены кэша продукта.")
    if final.exists():
        if final.is_dir():
            shutil.rmtree(final)
        else:
            final.unlink()
    stage.replace(final)


def validate_saved_product(folder: Path, record: Any) -> dict[str, Any]:
    """Validate product/passport agreement, safe file names, and stored bytes."""
    if not isinstance(record, dict):
        raise ValueError("Запись сохранённого продукта повреждена.")
    identity = record.get("id")
    folder = Path(folder)
    root = folder.parent.resolve()
    metadata = folder / "product.json"
    if (not isinstance(identity, str) or not _IDENTITY.fullmatch(identity)
            or folder.name != identity or folder.is_symlink() or not folder.is_dir()
            or folder.resolve().parent != root or metadata.is_symlink()
            or (metadata.exists() and metadata.resolve().parent != folder.resolve())):
        raise ValueError("Идентификатор продукта не совпадает с каталогом.")
    legend = record.get("legend")
    grid_record = record.get("grid")
    if not isinstance(legend, dict) or not isinstance(grid_record, dict):
        raise ValueError("Поля legend и grid сохранённого продукта должны быть объектами.")
    passport = validate_product_passport(record.get("passport"))
    if (passport.get("product_id") != record.get("product")
            or passport.get("valid_time") != record.get("time")
            or passport.get("units") != legend.get("units")
            or passport.get("calibration", {}).get("status") != record.get("calibration_status")
            or passport.get("algorithm", {}).get("version") != legend.get("version")
            or passport.get("grid", {}).get("crs") != grid_record.get("crs")
            or passport.get("grid", {}).get("width") != grid_record.get("width")
            or passport.get("grid", {}).get("height") != grid_record.get("height")
            or passport.get("grid", {}).get("transform") != record.get("transform")
            or passport.get("grid", {}).get("bounds") != grid_record.get("bounds")
            or passport.get("grid", {}).get("resolution") != _resolution(record.get("transform"))
            or passport.get("grid", {}).get("resolution_units") != _resolution_units(grid_record.get("crs"))
            or passport.get("nodata") != legend.get("nodata")
            or passport.get("source_grids") != record.get("source_grids")
            or not _qc_matches(passport.get("quality_control"), legend)):
        raise ValueError("Паспорт продукта противоречит product.json.")
    names = record.get("files")
    if (not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names)
            or len(names) != len(set(names)) or "product.json" not in names):
        raise ValueError("Список файлов продукта повреждён.")
    for name in names:
        _safe_name(name)
    declared = record.get("artifacts")
    expected_names = set(names) - {"product.json"}
    if not isinstance(declared, dict) or set(declared) != expected_names:
        raise ValueError("Манифест артефактов продукта неполон.")
    root = folder.resolve()
    for name in expected_names:
        entry = declared.get(name)
        path = folder / name
        if (not isinstance(entry, dict) or path.is_symlink() or not path.is_file()
                or path.resolve().parent != root
                or entry.get("sha256") != _sha256(path)
                or isinstance(entry.get("size_bytes"), bool)
                or entry.get("size_bytes") != path.stat().st_size):
            raise ValueError("Артефакт продукта повреждён или изменён: {}.".format(name))
    return record


def _resolution(transform):
    if not isinstance(transform, (list, tuple)) or len(transform) != 6:
        return None
    return [transform[0], transform[4]]


def _resolution_units(crs):
    if not isinstance(crs, str):
        return None
    return "degrees" if crs.upper() == "EPSG:4326" else "m"


def _qc_matches(quality_control, legend):
    if not isinstance(quality_control, dict) or not isinstance(legend, dict):
        return False
    return (quality_control.get("flags") == legend.get("quality_flags")
            and quality_control.get("valid_pixels") == legend.get("valid_pixels")
            and quality_control.get("total_pixels") == legend.get("total_pixels"))


def _safe_name(name: Any) -> None:
    if not isinstance(name, str) or name in (".", "..") or not _ARTIFACT.fullmatch(name):
        raise ValueError("Имя артефакта продукта небезопасно.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
