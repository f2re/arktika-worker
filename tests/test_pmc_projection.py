"""Synthetic numerical contracts; projection scale and meteorology are unvalidated."""
import unittest

import numpy as np
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds

from arktika.pmc_projection import reproject_observation_masks, reproject_vorticity_to_grid as _reproject_vorticity_to_grid


def reproject_vorticity_to_grid(values, latitude, longitude, *, target_grid, source_passport=None):
    source_passport = source_passport or {
        "source": "era5", "valid_time": "2024-01-01T00:00:00Z",
        "algorithm_id": "test-vorticity", "algorithm_version": "1.0.0",
        "grid": {"crs": "EPSG:4326", "grid_id": "fixture"},
    }
    return _reproject_vorticity_to_grid(values, latitude, longitude,
                                        target_grid=target_grid, source_passport=source_passport)


def target_grid(lat, lon, *, crs="EPSG:3857", size=80):
    bounds = transform_bounds("EPSG:4326", crs, float(lon.min()), float(lat.min()),
                              float(lon.max()), float(lat.max()), densify_pts=21)
    transform = from_bounds(*bounds, size, size)
    return {"crs": crs, "width": size, "height": size, "transform": transform,
            "resolution_x_m": abs(transform.a), "resolution_y_m": abs(transform.e)}


class PmcProjectionTests(unittest.TestCase):
    def setUp(self):
        self.lat = np.linspace(68.0, 73.0, 51)
        self.lon = np.linspace(20.0, 25.0, 51)
        self.grid = target_grid(self.lat, self.lon)

    def test_reprojects_linear_longitude_scalar_and_populates_detector_grid(self):
        values = np.broadcast_to(1.0 + 2.0 * self.lon[None, :], (self.lat.size, self.lon.size)).copy()
        src_passport = {"source": "era5", "source_version": "synthetic-v1",
                        "valid_time": "2024-01-01T00:00:00Z", "algorithm_id": "test-vorticity",
                        "algorithm_version": "test-v1",
                        "grid": {"crs": "EPSG:4326", "grid_id": "fixture"}}
        result = reproject_vorticity_to_grid(values, self.lat, self.lon,
                                             target_grid=self.grid, source_passport=src_passport)
        self.assertEqual(result["values"].shape, (80, 80))
        self.assertEqual(result["grid"]["crs"], "EPSG:3857")
        self.assertEqual(result["grid"]["width"], 80)
        self.assertEqual(result["grid"]["height"], 80)
        self.assertAlmostEqual(result["grid"]["resolution_x_m"], abs(result["grid"]["transform"][0]))
        self.assertAlmostEqual(result["grid"]["resolution_y_m"], abs(result["grid"]["transform"][4]))
        self.assertEqual(len(result["grid"]["transform"]), 6)
        self.assertIsInstance(result["grid"]["transform"], list)
        self.assertEqual(result["passport"]["source"], "era5")
        self.assertEqual(result["passport"]["valid_time"], src_passport["valid_time"])
        self.assertEqual(result["passport"]["grid"], result["grid"])
        self.assertEqual(result["passport"]["source_grid"]["grid_id"] if "grid_id" in result["passport"]["source_grid"] else "fixture", "fixture")
        self.assertEqual(result["passport"]["variable"], "relative_vorticity")
        self.assertEqual(result["passport"]["level_hPa"], 850)
        self.assertEqual(result["passport"]["units"], "s-1")
        self.assertEqual(result["passport"]["maturity"], "experimental")
        self.assertTrue(np.isfinite(result["values"]).any())
        self.assertGreaterEqual(float(np.nanmin(result["values"])), 41.0)
        self.assertLessEqual(float(np.nanmax(result["values"])), 51.0)

    def test_constant_field_is_preserved(self):
        field = np.full((self.lat.size, self.lon.size), 3.25, dtype=np.float32)
        result = reproject_vorticity_to_grid(field, self.lat, self.lon, target_grid=self.grid)
        np.testing.assert_allclose(result["values"][np.isfinite(result["values"])], 3.25, atol=1e-5)

    def test_nodata_hole_and_outside_support_remain_missing(self):
        field = np.ones((self.lat.size, self.lon.size), dtype=np.float32)
        field[25, 25] = np.nan
        result = reproject_vorticity_to_grid(field, self.lat, self.lon, target_grid=self.grid)
        missing = ~np.isfinite(result["values"])
        self.assertTrue(missing.any())
        self.assertTrue(missing[35:46, 35:46].any())
        self.assertTrue(np.isfinite(result["values"]).any())

    def test_accepts_rectilinear_mesh(self):
        lat2, lon2 = np.meshgrid(self.lat, self.lon, indexing="ij")
        result = reproject_vorticity_to_grid(np.ones_like(lat2), lat2, lon2, target_grid=self.grid)
        self.assertTrue(np.isfinite(result["values"]).any())

    def test_rectangular_source_records_coordinate_digest(self):
        lat = np.linspace(65.0, 70.0, 33)
        lon = np.linspace(-20.0, -10.0, 51)
        grid = target_grid(lat, lon, crs="EPSG:3413", size=40)
        field = np.ones((lat.size, lon.size), dtype=np.float32)
        result = reproject_vorticity_to_grid(field, lat, lon, target_grid=grid)
        self.assertEqual(result["passport"]["source_grid"]["width"], 51)
        self.assertEqual(result["passport"]["source_grid"]["height"], 33)
        self.assertEqual(len(result["passport"]["source_grid"]["coordinate_sha256"]), 64)

    def test_unwraps_dateline_and_records_source_affine(self):
        lat = np.linspace(65, 70, 11)
        lon = np.array([178, 178.4, 178.8, 179.2, 179.6, -180, -179.6, -179.2, -178.8, -178.4, -178])
        grid = target_grid(lat, np.array([178.0, 182.0]))
        grid["width"] = grid["height"] = 20
        grid["transform"] = from_bounds(*transform_bounds("EPSG:4326", "EPSG:3857", 178, 65, 182, 70), 20, 20)
        grid["resolution_x_m"] = abs(grid["transform"].a)
        grid["resolution_y_m"] = abs(grid["transform"].e)
        result = reproject_vorticity_to_grid(np.ones((11, 11)), lat, lon, target_grid=grid)
        self.assertGreater(result["passport"]["source_grid"]["transform"][0], 0)
        self.assertAlmostEqual(result["passport"]["source_grid"]["transform"][2], 177.8)

    def test_rejects_irregular_rectilinear_sources(self):
        irregular = self.lon.copy(); irregular[20] += 0.01
        with self.assertRaisesRegex(ValueError, "uniform spacing"):
            reproject_vorticity_to_grid(np.ones((51, 51)), self.lat, irregular, target_grid=self.grid)

    def test_reprojects_structured_curvilinear_mesh_from_geolocation_arrays(self):
        lat, lon = np.meshgrid(np.linspace(68, 73, 51), np.linspace(20, 25, 51), indexing="ij")
        lon = lon + 0.02 * np.sin(np.deg2rad(lat - 68))
        lat = lat + 0.015 * np.sin(np.deg2rad(lon - 20))
        grid = target_grid(lat, lon, crs="EPSG:3413", size=64)
        source = np.ones_like(lat, dtype=np.float32)
        source_passport = {"source": "carra2", "valid_time": "2024-01-01T00:00:00Z",
                           "algorithm_id": "test-vorticity", "algorithm_version": "test-v2",
                           "grid": {"crs": "EPSG:4326", "transform": [1, 0, 0, 0, -1, 0]}}
        result = reproject_vorticity_to_grid(source, lat, lon, target_grid=grid,
                                             source_passport=source_passport)
        self.assertEqual(result["passport"]["source_grid"]["grid_type"],
                         "curvilinear_latitude_longitude")
        self.assertEqual(result["passport"]["source_grid"]["geolocation"],
                         "latitude_longitude_arrays")
        self.assertNotIn("transform", result["passport"]["source_grid"])
        self.assertEqual(result["passport"]["reprojection"]["source_georeferencing"],
                         "2d_latitude_longitude_geolocation_arrays")
        np.testing.assert_allclose(result["values"][np.isfinite(result["values"])], 1.0, atol=1e-5)

    def test_rejects_nonprojected_crs_and_rotated_or_singular_affines(self):
        bad = dict(self.grid, crs="EPSG:4326")
        with self.assertRaisesRegex(ValueError, "projected"):
            reproject_vorticity_to_grid(np.ones((51, 51)), self.lat, self.lon, target_grid=bad)
        rotated = dict(self.grid, transform=[self.grid["transform"].a, 1, self.grid["transform"].c,
                                             0, self.grid["transform"].e, self.grid["transform"].f])
        with self.assertRaisesRegex(ValueError, "rotated"):
            reproject_vorticity_to_grid(np.ones((51, 51)), self.lat, self.lon, target_grid=rotated)
        singular = dict(self.grid, transform=[0, 0, 0, 0, 0, 0])
        with self.assertRaisesRegex(ValueError, "nonsingular"):
            reproject_vorticity_to_grid(np.ones((51, 51)), self.lat, self.lon, target_grid=singular)

    def test_rejects_resolution_dimension_mismatch(self):
        bad = dict(self.grid, resolution_x_m=self.grid["resolution_x_m"] * 2)
        with self.assertRaisesRegex(ValueError, "resolutions"):
            reproject_vorticity_to_grid(np.ones((51, 51)), self.lat, self.lon, target_grid=bad)

    def test_rejects_fractional_or_boolean_dimensions(self):
        for bad_width in (80.5, True):
            bad = dict(self.grid, width=bad_width)
            with self.assertRaisesRegex(ValueError, "target grid metadata is invalid"):
                reproject_vorticity_to_grid(np.ones((51, 51)), self.lat, self.lon, target_grid=bad)

    def test_requires_source_passport(self):
        with self.assertRaisesRegex(ValueError, "complete source passport"):
            _reproject_vorticity_to_grid(np.ones((51, 51)), self.lat, self.lon,
                                         target_grid=self.grid, source_passport=None)

    def test_binary_observation_masks_reproject_with_nearest_and_preserve_unknown_coverage(self):
        source = {"crs": "EPSG:3413", "width": 2, "height": 2,
                  "transform": [1000, 0, 0, 0, -1000, 2000],
                  "resolution_x_m": 1000.0, "resolution_y_m": 1000.0}
        target = {"crs": "EPSG:3413", "width": 4, "height": 4,
                  "transform": [500, 0, 0, 0, -500, 2000],
                  "resolution_x_m": 500.0, "resolution_y_m": 500.0}
        labels = np.array([[0, 1], [1, 0]], dtype=np.uint8)
        coverage = np.array([[1, 1], [1, 0]], dtype=np.uint8)
        projected, observed, provenance = reproject_observation_masks(
            labels, coverage, source_grid=source, target_grid=target)
        self.assertEqual(provenance["method"], "nearest_neighbor")
        self.assertEqual(provenance["source_grid"]["width"], 2)
        self.assertEqual(provenance["target_grid"]["width"], 4)
        self.assertEqual(projected.dtype, np.uint8)
        self.assertTrue(np.isin(projected, (0, 1)).all())
        self.assertTrue(np.isin(observed, (0, 1)).all())
        self.assertTrue(np.all(projected[observed == 0] == 0))
        self.assertTrue((observed == 0).any())
        self.assertTrue((projected == 1).any())

    def test_observation_reprojection_leaves_outside_footprint_unknown(self):
        source = {"crs": "EPSG:3413", "width": 2, "height": 2,
                  "transform": [1000, 0, 0, 0, -1000, 2000],
                  "resolution_x_m": 1000.0, "resolution_y_m": 1000.0}
        target = {"crs": "EPSG:3413", "width": 4, "height": 2,
                  "transform": [1000, 0, -1000, 0, -1000, 2000],
                  "resolution_x_m": 1000.0, "resolution_y_m": 1000.0}
        labels = np.ones((2, 2), dtype=np.uint8)
        coverage = np.ones((2, 2), dtype=np.uint8)
        projected, observed, _ = reproject_observation_masks(
            labels, coverage, source_grid=source, target_grid=target)
        np.testing.assert_array_equal(observed[:, (0, 3)], 0)
        np.testing.assert_array_equal(projected[:, (0, 3)], 0)
        self.assertTrue((observed[:, 1:3] == 1).all())

    def test_observation_source_grid_accepts_a_rotated_affine(self):
        resolution = float(np.hypot(1000, 50))
        source = {"crs": "EPSG:3413", "width": 2, "height": 2,
                  "transform": [1000, 50, 0, 50, -1000, 2000],
                  "resolution_x_m": resolution, "resolution_y_m": resolution}
        target = {"crs": "EPSG:3413", "width": 4, "height": 4,
                  "transform": [500, 0, 0, 0, -500, 2000],
                  "resolution_x_m": 500.0, "resolution_y_m": 500.0}
        labels = np.array([[0, 1], [1, 0]], dtype=np.uint8)
        coverage = np.ones((2, 2), dtype=np.uint8)
        projected, observed, provenance = reproject_observation_masks(
            labels, coverage, source_grid=source, target_grid=target)
        self.assertEqual(provenance["method"], "nearest_neighbor")
        self.assertEqual(provenance["source_grid"]["transform"], source["transform"])
        self.assertTrue((observed == 1).any())
        self.assertTrue(np.isin(projected, (0, 1)).all())

    def test_observation_mask_identity_and_invalid_label_coverage_are_checked(self):
        grid = {"crs": "EPSG:3413", "width": 2, "height": 2,
                "transform": [1000, 0, 0, 0, -1000, 2000],
                "resolution_x_m": 1000.0, "resolution_y_m": 1000.0}
        labels = np.array([[0, 1], [1, 0]], dtype=np.uint8)
        coverage = np.array([[1, 1], [1, 0]], dtype=np.uint8)
        same_labels, same_coverage, provenance = reproject_observation_masks(
            labels, coverage, source_grid=grid, target_grid=dict(grid))
        self.assertEqual(provenance["method"], "identity")
        np.testing.assert_array_equal(same_labels, labels)
        np.testing.assert_array_equal(same_coverage, coverage)
        coverage[0, 1] = 0
        with self.assertRaisesRegex(ValueError, "outside the coverage"):
            reproject_observation_masks(labels, coverage, source_grid=grid, target_grid=grid)


if __name__ == "__main__":
    unittest.main()
