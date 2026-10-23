"""Запуск доверенных адаптеров над проверенными локальными полями."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid

import numpy as np

from .adapters import SourceData
from .download import atomic_json, digest

_FIELD_ID = re.compile(r"^[0-9a-f]{24}$")
_COMPONENTS = {"u": "eastward_wind", "v": "northward_wind"}


class DerivedProductServiceMixin:
    def run_derived_adapter(self, data):
        """Run only registered code over source fields resolved by cache ID."""
        if not isinstance(data, dict) or set(data) != {"adapter_id", "inputs"}:
            raise ValueError("Нужны только идентификатор адаптера и ID входных полей.")
        adapter_id = data.get("adapter_id")
        adapter = self.derived_adapters.get(adapter_id)
        spec = adapter.spec
        input_ids = data.get("inputs")
        if (not isinstance(input_ids, dict) or set(input_ids) != set(spec.input_ids)
                or any(not isinstance(value, str) or not _FIELD_ID.fullmatch(value)
                       for value in input_ids.values())):
            raise ValueError("Укажите ID кэшированных полей для всех входов адаптера.")

        import xarray as xr
        inputs = {}
        geolocation = None
        grid_signature = None
        time_signature = None
        for input_id in spec.input_ids:
            field_id = input_ids[input_id]
            metadata = self.field(field_id)
            path = self.field_cache / field_id / "field.nc"
            before = digest(path)
            if before != metadata.get("sha256"):
                raise ValueError("Контрольная сумма поля не совпадает с кэш-карточкой.")
            try:
                with xr.open_dataset(path) as dataset:
                    expected_attrs = {
                        "source": metadata.get("source"),
                        "variable": metadata.get("variable"),
                        "time_kind": metadata.get("time_kind"),
                        "geolocation_crs": "EPSG:4326",
                    }
                    for key, expected in expected_attrs.items():
                        if dataset.attrs.get(key) != expected:
                            raise ValueError("Атрибуты NetCDF не совпадают с паспортом кэша.")
                    time = dataset.coords.get("time")
                    if time is None or time.ndim != 0:
                        raise ValueError("В поле нужен один точный срок UTC.")
                    valid_time = np.datetime_as_string(
                        np.asarray(time.values).astype("datetime64[ns]"), unit="s"
                    ) + "Z"
                    if valid_time != metadata.get("time"):
                        raise ValueError("Срок NetCDF не совпадает с кэш-карточкой.")
                    if "pressure_level" in dataset.coords:
                        level = dataset["pressure_level"]
                        if (level.ndim != 0 or level.attrs.get("units") != "hPa"
                                or metadata.get("level") is None
                                or float(level.values) != float(metadata["level"])):
                            raise ValueError("Уровень давления не совпадает с кэш-карточкой.")
                    variable_name = input_id if input_id in _COMPONENTS else "value"
                    variable = dataset.data_vars.get(variable_name)
                    if variable is None or variable.ndim != 2:
                        raise ValueError("Кэш не содержит двумерное поле для входа адаптера.")
                    expected_quantity = _COMPONENTS.get(input_id)
                    quantity = variable.attrs.get("standard_name")
                    units = variable.attrs.get("units")
                    if expected_quantity is not None and quantity != expected_quantity:
                        raise ValueError("Компонент ветра не подтверждён CF standard_name.")
                    if expected_quantity is not None and units != "m s-1":
                        raise ValueError("Компоненты ветра должны быть в m s-1.")
                    if not isinstance(quantity, str) or not quantity.strip():
                        raise ValueError("В NetCDF отсутствует CF standard_name величины.")
                    if not isinstance(units, str) or not units.strip():
                        raise ValueError("В NetCDF отсутствуют единицы величины.")
                    if variable_name == "value" and units != metadata.get("unit"):
                        raise ValueError("Единицы NetCDF не совпадают с кэш-карточкой.")
                    latitude = dataset.coords.get("latitude")
                    longitude = dataset.coords.get("longitude")
                    if (latitude is None or longitude is None or latitude.ndim != 2
                            or longitude.ndim != 2 or latitude.dims != variable.dims
                            or longitude.dims != variable.dims or latitude.shape != variable.shape
                            or longitude.shape != variable.shape
                            or latitude.attrs.get("standard_name") != "latitude"
                            or longitude.attrs.get("standard_name") != "longitude"):
                        raise ValueError("Геолокация должна быть подтверждённой 2-D широтой и долготой.")
                    lat = np.asarray(latitude.load().values, dtype="<f8")
                    lon = np.asarray(longitude.load().values, dtype="<f8")
                    if not np.isfinite(lat).all() or not np.isfinite(lon).all():
                        raise ValueError("Геолокация содержит нечисловые координаты.")
                    coordinate_digest = hashlib.sha256(
                        lat.tobytes(order="C") + lon.tobytes(order="C")
                    ).hexdigest()
                    regular = np.all(lat == lat[:, :1]) and np.all(lon == lon[:1, :])
                    grid = {
                        "crs": "EPSG:4326", "width": int(variable.shape[1]),
                        "height": int(variable.shape[0]),
                        "grid_type": "regular" if regular else "curvilinear",
                        "geolocation_sha256": coordinate_digest,
                        "coordinate_variables": ["latitude", "longitude"],
                        "geolocation": "latitude_longitude_arrays",
                    }
                    values = np.asarray(variable.load().values)
                    data_kind = "analysis" if metadata.get("time_kind") in (
                        "analysis", "instantaneous_analysis"
                    ) else "observation"
                    if data_kind != spec.data_kind:
                        raise ValueError("Тип входных данных не совпадает с контрактом адаптера.")
            except (OSError, KeyError, TypeError) as exc:
                raise ValueError("Не удалось проверить локальное поле для адаптера.") from exc
            after = digest(path)
            if before != after:
                raise ValueError("Кэшированное поле изменилось во время чтения.")
            current_signature = (grid["crs"], grid["width"], grid["height"],
                                 grid["grid_type"], grid["geolocation_sha256"])
            if grid_signature is not None and current_signature != grid_signature:
                raise ValueError("Входы адаптера должны иметь одну и ту же геолокационную сетку.")
            if time_signature is not None and valid_time != time_signature:
                raise ValueError("Входы адаптера должны иметь один точный срок UTC.")
            grid_signature, time_signature = current_signature, valid_time
            if geolocation is None:
                geolocation = (lat, lon)
            inputs[input_id] = SourceData(
                input_id=input_id,
                values=values,
                quantity=quantity,
                units=units,
                valid_time=valid_time,
                grid=grid,
                source={"id": metadata["source"], "dataset": metadata["variable"],
                        "field_id": field_id, "sha256": before},
                data_kind=data_kind,
            )

        result = self.derived_adapters.run(adapter_id, inputs)
        identity = uuid.uuid4().hex[:24]
        folder = self.derived_product_root / identity
        with tempfile.TemporaryDirectory(prefix=".derived-product-", dir=self.derived_product_root) as temporary:
            stage = Path(temporary)
            with (stage / "values.npz").open("wb") as output:
                np.savez_compressed(output, values=result.values,
                                    latitude=geolocation[0], longitude=geolocation[1])
            record = {
                "id": identity,
                "product_id": result.spec.product_id,
                "title": result.spec.title,
                "algorithm_id": result.spec.algorithm_id,
                "algorithm_version": result.spec.version,
                "maturity": result.spec.maturity,
                "quantity": result.spec.quantity,
                "units": result.spec.units,
                "time": result.valid_time,
                "files": ["values.npz", "product.json"],
                "grid": dict(result.grid),
                "passport": dict(result.passport),
                "output_status": "ready" if result.spec.maturity == "implemented" else "experimental",
                "storage_format": "numpy_npz_with_latitude_longitude_arrays",
            }
            atomic_json(stage / "product.json", record)
            os.replace(stage, folder)
        return {"product": record, "download": "/derived-artifact/{}/values.npz".format(identity)}

    def derived_products(self):
        """List only derived numeric products, separate from map-ready imagery."""
        from .product_schema import validate_product_passport

        rows = []
        for path in self.derived_product_root.glob("*/product.json"):
            if not _FIELD_ID.fullmatch(path.parent.name):
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                validate_product_passport(record.get("passport"))
                rows.append(record)
            except (OSError, ValueError, TypeError):
                continue
        return sorted(rows, key=lambda item: (item["time"], item["id"]), reverse=True)

    def derived_product_artifact(self, identity, name):
        if not isinstance(identity, str) or not _FIELD_ID.fullmatch(identity):
            raise ValueError("Неверный идентификатор производного продукта.")
        if name != "values.npz":
            raise ValueError("У этого продукта нет такого файла.")
        path = self.derived_product_root / identity / "product.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            from .product_schema import validate_product_passport
            validate_product_passport(record.get("passport"))
        except (OSError, ValueError, TypeError):
            raise ValueError("Производный продукт не найден или его паспорт повреждён.") from None
        artifact = self.derived_product_root / identity / name
        if not artifact.is_file():
            raise ValueError("Файл производного продукта отсутствует.")
        return artifact
