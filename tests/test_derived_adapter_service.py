"""Интеграционные проверки доверенного API запуска адаптеров."""
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import xarray as xr

from arktika.download import digest
from arktika.workstation import Workstation


class DerivedAdapterServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app = Workstation(self.root / "state")
        self.app._fields_init()
        self.ids = {}
        lat = np.array([[70., 70.1], [71., 71.1]])
        lon = np.array([[20., 21.], [20., 21.]])
        for component, identity, values, standard_name in (
                ("u", "a" * 24, [[3., 0.], [5., np.nan]], "eastward_wind"),
                ("v", "b" * 24, [[4., 2.], [12., 1.]], "northward_wind")):
            folder = self.app.field_cache / identity
            folder.mkdir(parents=True)
            dataset = xr.Dataset(
                {component: (("y", "x"), np.asarray(values, dtype="float32"),
                             {"standard_name": standard_name, "units": "m s-1"})},
                coords={
                    "time": np.datetime64("2025-01-02T03:00:00"),
                    "latitude": (("y", "x"), lat, {"standard_name": "latitude", "units": "degrees_north"}),
                    "longitude": (("y", "x"), lon, {"standard_name": "longitude", "units": "degrees_east"}),
                },
                attrs={"source": "era5", "variable": "wind", "time_kind": "instantaneous_analysis",
                       "geolocation_crs": "EPSG:4326"},
            )
            path = folder / "field.nc"
            dataset.to_netcdf(path, engine="scipy")
            metadata = {"id": identity, "source": "era5", "variable": "wind",
                        "time_kind": "instantaneous_analysis", "geolocation_crs": "EPSG:4326",
                        "time": "2025-01-02T03:00:00Z", "unit": "m s-1",
                        "level": None, "sha256": digest(path)}
            (folder / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
            self.ids[component] = identity

    def tearDown(self):
        self.app.close()
        self.temporary.cleanup()

    def test_run_saves_verifiable_product_with_geolocation_and_provenance(self):
        result = self.app.run_derived_adapter({"adapter_id": "wind_speed", "inputs": self.ids})
        product = result["product"]
        self.assertEqual(product["output_status"], "ready")
        self.assertEqual(product["passport"]["grid"]["grid_type"], "curvilinear")
        self.assertEqual(product["passport"]["algorithm"]["version"], "1.0.0")
        self.assertEqual(len(product["passport"]["sources"]), 2)
        self.assertTrue(all("field_id" in source and "sha256" in source
                            for source in product["passport"]["sources"]))
        self.assertTrue(all(not any(secret in key.lower() for secret in ("path", "url", "token"))
                            for source in product["passport"]["sources"] for key in source))
        stored = self.app.derived_products()[0]
        self.assertEqual(stored["passport"], product["passport"])
        with np.load(self.app.derived_product_artifact(product["id"], "values.npz")) as values:
            np.testing.assert_allclose(values["values"], [[5., 2.], [13., np.nan]],
                                       rtol=1e-6, equal_nan=True)
            self.assertEqual(values["latitude"].shape, (2, 2))

    def test_rejects_paths_and_mismatched_input_contract(self):
        with self.assertRaises(ValueError):
            self.app.run_derived_adapter({"adapter_id": "wind_speed", "inputs": {
                "u": str(self.root / "arbitrary.nc"), "v": self.ids["v"]}})
        with self.assertRaises(ValueError):
            self.app.run_derived_adapter({"adapter_id": "wind_speed", "inputs": {
                "u": self.ids["u"]}})

    def test_rejects_changed_cached_field_before_running(self):
        metadata_path = self.app.field_cache / self.ids["u"] / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["sha256"] = "0" * 64
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.app.run_derived_adapter({"adapter_id": "wind_speed", "inputs": self.ids})

    def test_rejects_mismatched_pressure_levels(self):
        folder = self.app.field_cache / self.ids["u"]
        path = folder / "field.nc"
        with xr.open_dataset(path) as opened:
            dataset = opened.load().assign_coords(
                pressure_level=xr.DataArray(850., attrs={"units": "hPa"}))
        dataset.to_netcdf(path, engine="scipy")
        metadata_path = folder / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.update(level=850, sha256=digest(path))
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "одному уровню давления"):
            self.app.run_derived_adapter({"adapter_id": "wind_speed", "inputs": self.ids})

    def test_rejects_changed_saved_output(self):
        result = self.app.run_derived_adapter({"adapter_id": "wind_speed", "inputs": self.ids})
        artifact = self.app.derived_product_artifact(result["product"]["id"], "values.npz")
        artifact.write_bytes(artifact.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "повреждён"):
            self.app.derived_product_artifact(result["product"]["id"], "values.npz")


if __name__ == "__main__":
    unittest.main()
