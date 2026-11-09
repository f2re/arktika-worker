"""Cache-bound experimental vorticity integration contracts."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import xarray as xr
from affine import Affine
from rasterio.errors import RasterioError
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds

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

    def test_cache_to_projected_single_time_candidate_pipeline(self):
        grid = self._projected_grid()
        result = self.app.experimental_pmc_candidates(self.identity, target_grid=grid)
        self.assertEqual(result["maturity"], "experimental")
        self.assertEqual(result["source"], "era5")
        self.assertEqual(result["field_passport"]["grid"]["crs"], "EPSG:3413")
        self.assertEqual(result["field_passport"]["grid"]["width"], 32)
        self.assertEqual(result["field_passport"]["grid"]["height"], 32)
        self.assertTrue(all(item["classification"] == "candidate" for item in result["candidates"]))

    def test_russian_display_units_preserve_the_cf_wind_contract(self):
        # The real normalizer stores a localized display label at the top level
        # and SI units on each physical component. Hand-written old fixtures
        # used SI everywhere and hid a total failure of the real import path.
        self.metadata["unit"] = "м/с"
        self._edit_netcdf(unit="м/с")
        result = self.app.experimental_vorticity_850(self.identity)
        self.assertEqual(result["passport"]["units"], "s-1")
        with xr.open_dataset(self.path) as source:
            invalid = source.load()
        invalid["u"].attrs["units"] = "knots"
        invalid.to_netcdf(self.path, engine="scipy")
        self.metadata["sha256"] = digest(self.path)
        self._write_metadata()
        with self.assertRaisesRegex(ValueError, "Единицы"):
            self.app.experimental_vorticity_850(self.identity)

    @staticmethod
    def _projected_grid(width=32, height=32):
        bounds = transform_bounds("EPSG:4326", "EPSG:3413", 20.0, 68.0, 23.0, 70.0,
                                  densify_pts=21)
        transform = from_bounds(*bounds, width, height)
        return {"crs": "EPSG:3413", "width": width, "height": height,
                "transform": list(transform[:6]), "resolution_x_m": abs(transform.a),
                "resolution_y_m": abs(transform.e)}

    def _register_observation(self, grid, *, valid_time, **scan):
        def raster_bytes(values):
            with MemoryFile() as mem:
                with mem.open(driver="GTiff", count=1, dtype="uint8", crs=grid["crs"],
                              transform=Affine(*grid["transform"]), width=grid["width"],
                              height=grid["height"]) as raster:
                    raster.write(values, 1)
                return mem.read()

        shape = (grid["height"], grid["width"])
        coverage = np.ones(shape, dtype=np.uint8)
        coverage[:, :shape[1] // 3] = 0
        metadata = {
            "schema_version": "arktika.pmc_satellite_observation.v1",
            "observation_ref": "fixture-scene", "source": "synthetic",
            "product": "labels", "product_version": "1",
            "valid_time": valid_time, "grid": grid, "nodata": None,
            "label_convention": "0=no_pmc,1=pmc_candidate",
            "qc_status": "not_assessed", **scan,
        }
        return self.app.register_pmc_observation(
            raster_bytes(np.zeros(shape, dtype=np.uint8)), raster_bytes(coverage), metadata
        )["observation_id"]

    def test_explicit_satellite_scan_reprojects_large_imported_mask(self):
        source_grid = self._projected_grid(width=1025, height=1024)
        observation_id = self._register_observation(
            source_grid, valid_time="2024-01-01T00:10:00Z",
            scan_start_utc="2023-12-31T23:55:00Z", scan_end_utc="2024-01-01T00:15:00Z",
        )
        target_grid = self._projected_grid()
        with patch.object(self.app, "pmc_observations_state",
                          side_effect=AssertionError("explicit selection must bypass catalogue")):
            result = self.app.experimental_pmc_candidates(
                self.identity, target_grid=target_grid, observation_id=observation_id)
        comparison = result["satellite_comparison"]
        self.assertEqual(comparison["status"], "comparison_only")
        observation = comparison["observation"]
        self.assertEqual(observation["observation_id"], observation_id)
        self.assertEqual(observation["scan_start_utc"], "2023-12-31T23:55:00Z")
        self.assertEqual(observation["valid_time"], "2024-01-01T00:10:00Z")
        self.assertEqual(observation["regridding"]["method"], "nearest_neighbor")
        self.assertEqual(observation["regridding"]["source_grid"], source_grid)
        self.assertEqual(observation["grid"], target_grid)
        self.assertGreater(comparison["coverage"]["observed_pixels"], 0)
        self.assertGreater(comparison["coverage"]["unknown_pixels"], 0)

    def test_custom_thresholds_change_cached_wind_screen_and_mask_overlap(self):
        grid = self._projected_grid()
        observation_id = self._register_observation(grid, valid_time=self.metadata["time"])
        options = {"target_grid": grid, "observation_id": observation_id}
        default = self.app.experimental_pmc_candidates(self.identity, **options)
        thresholds = {"minimum_vorticity_s-1": 1e-5, "peak_vorticity_s-1": 2e-5}
        custom = self.app.experimental_pmc_candidates(self.identity, thresholds=thresholds, **options)

        self.assertEqual(default["candidate_count"], 0)
        self.assertEqual(custom["candidate_count"], 1)
        self.assertEqual(custom["candidates"][0]["confirmation_status"], "not_confirmed")
        self.assertEqual(custom["passport"]["quality_control"]["thresholds"], thresholds)
        default_comparison = default["satellite_comparison"]
        custom_comparison = custom["satellite_comparison"]
        self.assertEqual(custom_comparison["status"], "comparison_only")
        for key in ("valid_model_pixels", "observed_pixels", "unknown_pixels"):
            self.assertEqual(custom_comparison["coverage"][key], default_comparison["coverage"][key])
        self.assertGreater(custom_comparison["coverage"]["candidate_pixels_outside_coverage"], 0)
        self.assertEqual(default_comparison["metrics"]["false_positive_pixels"], 0)
        self.assertGreater(custom_comparison["metrics"]["false_positive_pixels"], 0)

    def test_explicit_satellite_outside_time_preserves_model_result(self):
        observation_id = self._register_observation(
            self._projected_grid(), valid_time="2024-01-01T01:00:00Z")
        grid = self._projected_grid()
        baseline = self.app.experimental_pmc_candidates(self.identity, target_grid=grid)
        result = self.app.experimental_pmc_candidates(
            self.identity, target_grid=grid, observation_id=observation_id)
        self.assertEqual(result["candidates"], baseline["candidates"])
        self.assertEqual(result["satellite_comparison"], {
            "status": "unknown_no_eligible_observation",
            "metrics": None,
            "reason_code": "no_eligible_observation",
        })
        self.assertEqual(result["passport"]["quality_control"]["satellite_comparison_status"],
                         "unknown_no_eligible_observation")

    def test_explicit_satellite_spatial_alignment_error_preserves_model_result(self):
        grid = self._projected_grid()
        observation_id = self._register_observation(grid, valid_time=self.metadata["time"])
        baseline = self.app.experimental_pmc_candidates(self.identity, target_grid=grid)
        with patch("arktika.reanalysis_service.reproject_observation_masks",
                   side_effect=RasterioError("synthetic alignment failure")):
            result = self.app.experimental_pmc_candidates(
                self.identity, target_grid=grid, observation_id=observation_id)
        self.assertEqual(result["candidates"], baseline["candidates"])
        self.assertEqual(result["satellite_comparison"], {
            "status": "unknown_unmatched", "metrics": None,
            "reason_code": "observation_grid_unavailable",
        })

    def test_explicit_missing_observation_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "не найдено"):
            self.app.experimental_pmc_candidates(
                self.identity, target_grid=self._projected_grid(), observation_id="b" * 24)

    def test_curvilinear_carra_cache_runs_single_time_experimental_path(self):
        with xr.open_dataset(self.path) as source:
            ds = source.load()
        lat, lon = np.meshgrid(np.linspace(68.0, 70.0, 3), np.linspace(20.0, 23.0, 4), indexing="ij")
        lon = lon + 0.015 * np.sin(np.deg2rad(lat - 68.0))
        lat = lat + 0.01 * np.sin(np.deg2rad(lon - 20.0))
        ds["latitude"] = (("y", "x"), lat,
                          {"units": "degrees_north", "standard_name": "latitude"})
        ds["longitude"] = (("y", "x"), lon,
                           {"units": "degrees_east", "standard_name": "longitude"})
        ds.attrs.update(source="carra2", wind_reference="geographic_from_direction")
        ds.to_netcdf(self.path, engine="scipy")
        self.metadata.update(source="carra2", sha256=digest(self.path))
        self._write_metadata()
        bounds = transform_bounds("EPSG:4326", "EPSG:3413", 20.0, 68.0, 23.0, 70.0,
                                  densify_pts=21)
        transform = from_bounds(*bounds, 32, 32)
        grid = {"crs": "EPSG:3413", "width": 32, "height": 32,
                "transform": transform, "resolution_x_m": abs(transform.a),
                "resolution_y_m": abs(transform.e)}
        result = self.app.experimental_pmc_candidates(self.identity, target_grid=grid)
        self.assertEqual(result["source"], "carra2")
        self.assertEqual(result["field_passport"]["source_grid"]["grid_type"],
                         "curvilinear_latitude_longitude")
        self.assertEqual(result["field_passport"]["reprojection"]["source_georeferencing"],
                         "2d_latitude_longitude_geolocation_arrays")
        self.assertTrue(any("CARRA2 использует ERA5" in item
                            for item in result["field_passport"]["limitations"]))

    def test_candidate_pipeline_rejects_projection_scale_distortion(self):
        bounds = transform_bounds("EPSG:4326", "EPSG:3857", 20.0, 68.0, 23.0, 70.0,
                                  densify_pts=21)
        transform = from_bounds(*bounds, 32, 32)
        grid = {"crs": "EPSG:3857", "width": 32, "height": 32,
                "transform": transform, "resolution_x_m": abs(transform.a),
                "resolution_y_m": abs(transform.e)}
        with self.assertRaisesRegex(ValueError, "Искажение масштаба"):
            self.app.experimental_pmc_candidates(self.identity, target_grid=grid)

    def test_candidate_pipeline_rejects_target_without_source_coverage(self):
        bounds = transform_bounds("EPSG:4326", "EPSG:3413", 20.0, 60.0, 23.0, 61.0,
                                  densify_pts=21)
        transform = from_bounds(*bounds, 32, 32)
        grid = {"crs": "EPSG:3413", "width": 32, "height": 32,
                "transform": transform, "resolution_x_m": abs(transform.a),
                "resolution_y_m": abs(transform.e)}
        with self.assertRaisesRegex(ValueError, "нет валидного покрытия"):
            self.app.experimental_pmc_candidates(self.identity, target_grid=grid)

    def test_rejects_wrong_source_and_metadata(self):
        self.metadata["source"] = "carra2"
        self._write_metadata()
        with self.assertRaisesRegex(ValueError, "Метаданные NetCDF не совпадают"):
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

    def test_accepts_curvilinear_grid_with_verified_cache_metadata(self):
        with xr.open_dataset(self.path) as source:
            ds = source.load()
        ds["latitude"].values[1, 1] += .1
        ds.to_netcdf(self.path, engine="scipy")
        self.metadata["sha256"] = digest(self.path)
        self._write_metadata()
        result = self.app.experimental_vorticity_850(self.identity)
        self.assertEqual(result["passport"]["grid"]["grid_type"],
                         "curvilinear_latitude_longitude")


if __name__ == "__main__":
    unittest.main()
