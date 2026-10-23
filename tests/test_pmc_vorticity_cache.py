"""Cache-bound experimental vorticity integration contracts."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import xarray as xr

from arktika.download import digest
from arktika.workstation import Workstation


class CachedVorticity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Workstation(self.root / "state")
        self.app._fields_init()
        self.identity = "a" * 24
        self.folder = self.app.field_cache / self.identity
        self.folder.mkdir(parents=True)
        latitude = np.array([68., 69., 70.])
        longitude = np.array([20., 21., 22., 23.])
        u = np.zeros((3, 4), dtype="float32")
        v = np.broadcast_to(np.arange(4, dtype="float32"), (3, 4)).copy()
        ds = xr.Dataset(
            {"value": (("y", "x"), np.hypot(u, v),
                       {"units": "m s-1", "standard_name": "wind_speed"}),
             "u": (("y", "x"), u, {"units": "m s-1", "standard_name": "eastward_wind"}),
             "v": (("y", "x"), v, {"units": "m s-1", "standard_name": "northward_wind"}),
             "latitude": (("y", "x"), np.broadcast_to(latitude[:, None], (3, 4)),
                          {"units": "degrees_north", "standard_name": "latitude"}),
             "longitude": (("y", "x"), np.broadcast_to(longitude[None, :], (3, 4)),
                           {"units": "degrees_east", "standard_name": "longitude"})},
            attrs={"source": "era5", "variable": "wind", "unit": "m s-1",
                   "time": "2024-01-01T00:00:00Z", "level": "850",
                   "time_kind": "instantaneous_analysis", "wind_reference": "east_north",
                   "geolocation_crs": "EPSG:4326",
                   "version": "normalizer-1"},
        ).set_coords(["latitude", "longitude"]).assign_coords(
            time=np.datetime64("2024-01-01T00:00:00", "ns"),
            pressure_level=((), 850., {"units": "hPa"}),
        )
        self.path = self.folder / "field.nc"
        ds.to_netcdf(self.path, engine="scipy")
        self.metadata = {"id": self.identity, "source": "era5", "variable": "wind",
                         "unit": "m s-1", "time": "2024-01-01T00:00:00Z",
                         "level": 850, "time_kind": "instantaneous_analysis",
                         "version": "normalizer-1", "sha256": digest(self.path)}
        self._write_metadata()

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def _write_metadata(self):
        (self.folder / "metadata.json").write_text(
            json.dumps(self.metadata), encoding="utf-8")

    def _edit_netcdf(self, **changes):
        with xr.open_dataset(self.path) as source:
            ds = source.load()
        ds.attrs.update(changes)
        ds.to_netcdf(self.path, engine="scipy")
        self.metadata["sha256"] = digest(self.path)
        self._write_metadata()

    def test_verified_cache_returns_experimental_scalar_and_clean_passport(self):
        result = self.app.experimental_vorticity_850(self.identity)
        self.assertEqual(result["values"].shape, (3, 4))
        self.assertEqual(result["passport"]["grid"]["crs"], "EPSG:4326")
        self.assertEqual(result["passport"]["maturity"], "experimental")
        self.assertEqual(result["passport"]["source_file_digest_attestation"],
                         "verified against cached field.nc digest before and after loading")
        self.assertEqual(result["passport"]["source_normalizer_version"], "normalizer-1")
        self.assertEqual(result["passport"]["source_version"],
                         "unavailable_from_normalized_cache")
        self.assertNotIn(str(self.root), json.dumps(result["passport"]))

    def test_rejects_wrong_source_and_metadata(self):
        self.metadata["source"] = "carra2"
        self._write_metadata()
        with self.assertRaisesRegex(ValueError, "ERA5 и MERRA-2"):
            self.app.experimental_vorticity_850(self.identity)
        self.metadata["source"] = "era5"
        self.metadata["level"] = 700
        self._write_metadata()
        with self.assertRaisesRegex(ValueError, "850 гПа"):
            self.app.experimental_vorticity_850(self.identity)

    def test_rejects_time_mismatch_between_cache_manifest_and_netcdf(self):
        self.metadata["time"] = "2024-01-01T03:00:00Z"
        self._write_metadata()
        with self.assertRaisesRegex(ValueError, "Срок UTC"):
            self.app.experimental_vorticity_850(self.identity)

    def test_rejects_incorrect_manifest_sha256(self):
        self.metadata["sha256"] = "0" * 64
        self._write_metadata()
        with self.assertRaisesRegex(ValueError, "Контрольная сумма"):
            self.app.experimental_vorticity_850(self.identity)

    def test_rejects_file_digest_change_during_load(self):
        changed = "f" * 64
        with patch("arktika.workstation.digest", side_effect=[
                self.metadata["sha256"], changed]):
            with self.assertRaisesRegex(ValueError, "изменилось во время чтения"):
                self.app.experimental_vorticity_850(self.identity)

    def test_rejects_missing_geographic_wind_attestation(self):
        self._edit_netcdf(wind_reference="")
        with self.assertRaisesRegex(ValueError, "географическая система"):
            self.app.experimental_vorticity_850(self.identity)

    def test_rejects_missing_component_metadata(self):
        with xr.open_dataset(self.path) as source:
            ds = source.load()
        ds["u"].attrs.pop("standard_name")
        ds.to_netcdf(self.path, engine="scipy")
        self.metadata["sha256"] = digest(self.path)
        self._write_metadata()
        with self.assertRaisesRegex(ValueError, "standard_name"):
            self.app.experimental_vorticity_850(self.identity)

    def test_rejects_misaligned_component_dimensions(self):
        with xr.open_dataset(self.path) as source:
            ds = source.load()
        ds["v"] = ds["v"].transpose("x", "y")
        ds.to_netcdf(self.path, engine="scipy")
        self.metadata["sha256"] = digest(self.path)
        self._write_metadata()
        with self.assertRaisesRegex(ValueError, "одинаково упорядоченную двумерную сетку"):
            self.app.experimental_vorticity_850(self.identity)

    def test_rejects_curvilinear_grid(self):
        with xr.open_dataset(self.path) as source:
            ds = source.load()
        ds["latitude"].values[1, 1] += .1
        ds.to_netcdf(self.path, engine="scipy")
        self.metadata["sha256"] = digest(self.path)
        self._write_metadata()
        with self.assertRaisesRegex(ValueError, "curvilinear"):
            self.app.experimental_vorticity_850(self.identity)


if __name__ == "__main__":
    unittest.main()
