"""Сохранение одной версии нативного кадра GPTL вместе с паспортами."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import threading
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .download import atomic_json, digest, download
from .gptl_native import parse_native_item
from .network import Client, NetworkError, S3_HOSTS, redact


METADATA_LIMIT = 8 * 1024**2
OBJECT_LIMIT = 512 * 1024**2
WINDOW_PIXELS = 262_144


def _read_file(path, limit, *, private=False):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if Path(path).is_symlink():
        raise ValueError("Входной файл не должен быть символьной ссылкой.")
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ValueError("Нужен обычный непустой файл в пределах ограничения размера.")
        if private and os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("Файл токена должен принадлежать исполнителю и быть доступен только ему.")
        raw = stream.read(limit + 1)
        if len(raw) != info.st_size:
            raise ValueError("Размер входного файла изменился при чтении.")
    return raw


def _write_exact(path, raw):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _metadata_item(client, item_file, item_url, cancel):
    if item_file is not None:
        return _read_file(item_file, METADATA_LIMIT)
    url = client.validate(item_url)
    if url.hostname not in S3_HOSTS | {"api.gptl.ru"}:
        raise ValueError("Исходную запись STAC нужно получить из хранилища или API GPTL.")
    if url.query and set(parse_qs(url.query, keep_blank_values=True)) != {"versionId"}:
        raise ValueError("В адресе исходного STAC допустим только параметр версии объекта.")
    raw = client.object(item_url, cancel=cancel, maximum=METADATA_LIMIT)["body"]
    if not raw:
        raise ValueError("GPTL вернул пустую запись STAC.")
    return raw


def _asset_on_disk(asset):
    # Полная ссылка может содержать временную подпись. Оставляем только
    # адрес и неизменяемый идентификатор версии.
    uri = urlsplit(asset["uri"])
    version = asset.get("object_version")
    query = urlencode({"versionId": version}) if version else ""
    return {**asset, "uri": urlunsplit((uri.scheme, uri.netloc, uri.path, query, ""))}


def _reference(root, path, checksum=None):
    return {"path": path.relative_to(root).as_posix(), "sha256": checksum or digest(path)}


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _window_metadata(window):
    if window is None:
        return None
    row, col, height, width = window
    if min(row, col) < 0 or min(height, width) <= 0 or height * width > WINDOW_PIXELS:
        raise ValueError("Некорректные смещения или размеры нативного окна.")
    return dict(row_offset=row, column_offset=col, height=height, width=width)


def _geometry_field(value, name, height, width, valid):
    def finite(x):
        return type(x) in (int, float) and abs(x) <= 1e308 and math.isfinite(x) and valid(x)

    field = value.get(name)
    if isinstance(field, list):
        return len(field) == height and all(
            isinstance(row, list) and len(row) == width and all(finite(x) for x in row) for row in field
        )
    return finite(field)


def _check_candidate_files(raster, quality, band, geometry, window):
    import rasterio

    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_PAM_ENABLED="NO"):
        with rasterio.open(raster) as source, rasterio.open(quality) as mask:
            if (source.driver != "GTiff" or mask.driver != "GTiff"
                    or source.count != 1 or mask.count != 1 or not source.crs
                    or (source.shape, source.crs, source.transform) != (mask.shape, mask.crs, mask.transform)
                    or not mask.dtypes[0].startswith(("int", "uint"))
                    or mask.scales != (1.0,) or mask.offsets != (0.0,) or mask.nodata == 0):
                raise ValueError("Растр и исходный целочисленный QC должны иметь общие сетку и геопривязку.")
            if (source.units[0] not in (None, band["unit"])
                    or (source.scales != (1.0,) and not math.isclose(source.scales[0], band["scale"],
                                                                   rel_tol=1e-5, abs_tol=1e-8))
                    or (source.offsets != (0.0,) and not math.isclose(source.offsets[0], band["offset"],
                                                                    rel_tol=1e-5, abs_tol=1e-8))
                    or (source.nodata is not None and band.get("nodata") is not None
                        and source.nodata != band["nodata"])):
                raise ValueError("Единицы или калибровка GeoTIFF противоречат исходному STAC.")
            height, width = source.shape
            if window is not None:
                if (window["row_offset"] + window["height"] > height
                        or window["column_offset"] + window["width"] > width):
                    raise ValueError("Нативное окно выходит за пределы растра.")
                height, width = window["height"], window["width"]
            elif height * width > WINDOW_PIXELS or max(raster.stat().st_size, quality.stat().st_size) > 32 * 1024**2:
                raise ValueError("Для этого растра необходимо указать ограниченное нативное окно.")
    if (not isinstance(geometry, dict)
            or set(geometry) - {"reference", "view_zenith_deg", "footprint_km", "solar_zenith_deg"}
            or not isinstance(geometry.get("reference"), str) or not geometry["reference"].strip()
            or not _geometry_field(geometry, "view_zenith_deg", height, width, lambda x: 0 <= x < 90)
            or not _geometry_field(geometry, "footprint_km", height, width, lambda x: x > 0)
            or ("solar_zenith_deg" in geometry and not _geometry_field(
                geometry, "solar_zenith_deg", height, width, lambda x: 0 <= x <= 180))):
        raise ValueError("Нужна геометрия с источником, углом и размером пятна на сетке растра.")


def _metadata_json(raw):
    def unique(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                raise ValueError("Повторяющееся поле JSON геометрии.")
            output[key] = value
        return output

    def nonfinite(_):
        raise ValueError("Геометрия содержит неконечное значение.")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)


def capture(client, *, output, raster_key, quality_key, item_file=None, item_url=None,
            geometry_file=None, historical=False, window=None, max_object_bytes=OBJECT_LIMIT):
    """Сохранить исходные файлы и явно указать готовность пакета к отдельной проверке."""
    if (item_file is None) == (item_url is None):
        raise ValueError("Укажите файл или HTTPS-адрес исходной записи STAC.")
    if type(max_object_bytes) is not int or not 1 <= max_object_bytes <= OBJECT_LIMIT:
        raise ValueError("Ограничение размера объекта должно быть в пределах 1–536870912 байт.")
    window = _window_metadata(window)
    output = Path(output)
    output.mkdir(mode=0o700)
    cancel = threading.Event()
    status_path = output / "capture.json"
    item = output / "producer-item.json"
    asset_path = output / "worker-asset.json"
    qc_asset_path = output / "worker-quality-asset.json"
    raster = output / "raster" / "channel.tif"
    quality = output / "qc" / "quality.tif"
    geometry = output / "geometry.json"
    report = dict(schema="arktika-worker-gptl-capture-1", status="incomplete",
                  model_ready=False, historical=historical)

    def finish(status, reason=None):
        report["status"] = status
        if reason is not None:
            report["reason"] = reason
        atomic_json(status_path, report)
        return report

    phase = "producer_item"
    try:
        raw_item = _metadata_item(client, item_file, item_url, cancel)
        _write_exact(item, raw_item)
        report["producer_item"] = _reference(output, item, hashlib.sha256(raw_item).hexdigest())
        phase = "supplier_metadata"
        entry = parse_native_item(raw_item, raster_key, quality_key, historical=historical)
        report.update({name: value for name, value in entry.items() if name not in ("raster_asset", "quality_asset")})
        raster_asset, qc_asset = entry["raster_asset"], entry["quality_asset"]
        atomic_json(asset_path, _asset_on_disk(raster_asset))
        atomic_json(qc_asset_path, _asset_on_disk(qc_asset))

        refs = {"producer_item": report["producer_item"], "asset": _reference(output, asset_path)}
        report["quality_asset"] = _reference(output, qc_asset_path)
        local_times = {}
        for role, file, asset, receipt_role in (
            ("raster", raster, raster_asset, "download"),
            ("quality", quality, qc_asset, "quality_download"),
        ):
            phase = role
            receipt = download(client, {**asset, "filename": file.name}, file, lambda *_: None, cancel,
                               max_size=max_object_bytes)
            refs[role] = _reference(output, file, receipt["sha256"])
            refs[receipt_role] = _reference(output, file.with_name(file.name + ".download.json"))
            local_times[role] = receipt["time"]
            report["files"] = refs
            report["local_downloaded_at"] = local_times
            published = entry[f"{role}_published_at"]
            earlier_bound = _instant(entry["scan_end"])
            if published:
                earlier_bound = max(earlier_bound, _instant(published))
            if _instant(receipt["time"]) < earlier_bound:
                raise ValueError("Время локальной загрузки предшествует съёмке или публикации актива.")
        both_downloaded = max(local_times.values(), key=_instant)
        if entry["provider_ready_at"] and _instant(entry["provider_ready_at"]) > _instant(both_downloaded):
            raise ValueError("Время готовности поставщика позже завершения загрузки.")
        report["available_at"] = entry["available_at"] if historical else both_downloaded
        if geometry_file is None:
            return finish("captured", "Нет подтверждённой геометрии визирования.")
        phase = "geometry"
        raw_geometry = _read_file(geometry_file, METADATA_LIMIT)
        _check_candidate_files(raster, quality, raster_asset["raster_bands"][0],
                               _metadata_json(raw_geometry), window)
        _write_exact(geometry, raw_geometry)
        refs["geometry"] = _reference(output, geometry)

        calibration = entry["calibration"]
        if not calibration["revision"] or not entry["provider_ready_at"]:
            return finish("captured", "Нет документированной версии калибровки или времени готовности поставщика.")
        quality_evidence = entry["quality"]
        if quality_evidence["good_flag"] != 0:
            return finish("captured", "Импорт модели требует документированного нулевого флага пригодности QC.")
        timing = {key: entry[key] for key in ("scan_start", "scan_end", "provider_ready_at")}
        timing["reference"] = "GPTL STAC Item: properties; в архиве отдельно сверяется published каждого актива"
        if historical:
            timing["availability_basis"] = "provider_asset_publication"
        schema = "arktika-m-native-l2ir-1" if entry["platform"].startswith("ARCM") else "electro-l-native-l2ir-1"
        native = dict(schema=schema, files=refs,
                      calibration={key: calibration[key] for key in ("asset_key", "revision", "reference")},
                      quality={key: quality_evidence[key] for key in ("asset_key", "good_flag", "reference")}
                      | {"asset_id": qc_asset["id"]}, timing=timing)
        if window is not None:
            native["window"] = window
        phase = "native_manifest"
        native_path = output / "native.json"
        atomic_json(native_path, native)
        report["native_manifest"] = _reference(output, native_path)
        return finish("candidate")
    except (OSError, ValueError, NetworkError) as error:
        report["phase"] = phase
        return finish("incomplete", redact(error)[:500])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--item-file", type=Path, help="Оригинальный STAC Item в UTF-8.")
    source.add_argument("--item-url", help="HTTPS-адрес оригинальной записи в GPTL.")
    parser.add_argument("--raster-key", required=True, help="Ключ численного ИК-актива.")
    parser.add_argument("--quality-key", required=True, help="Ключ привязанной нативной маски QC.")
    parser.add_argument("--geometry", dest="geometry_file", type=Path, help="Проверенный локальный паспорт геометрии.")
    parser.add_argument("--output", type=Path, required=True, help="Новая отдельная папка для сохранения пакета.")
    parser.add_argument("--window", nargs=4, type=int, metavar=("ROW", "COL", "HEIGHT", "WIDTH"))
    parser.add_argument("--historical", action="store_true", help="Проверять готовность точных архивных версий.")
    parser.add_argument("--max-object-bytes", type=int, default=OBJECT_LIMIT)
    parser.add_argument("--token-file", type=Path, help="Личный файл Bearer с ограниченными правами доступа.")
    args = vars(parser.parse_args(argv))
    token_file = args.pop("token_file")
    env_token = os.environ.pop("GPTL_TOKEN", "")
    try:
        token = _read_file(token_file, 65536, private=True).decode("utf-8") if token_file else env_token
        report = capture(Client(token=token), **args)
    except (OSError, ValueError, NetworkError) as error:
        parser.exit(2, f"{redact(error)}\n")
    print(json.dumps({"status": report["status"], "model_ready": False}, ensure_ascii=False))
    return 0 if report["status"] == "candidate" else 2


if __name__ == "__main__":
    raise SystemExit(main())
