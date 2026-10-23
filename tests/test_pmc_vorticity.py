"""Synthetic numerical contracts only; no meteorological validation is implied."""
import unittest

import numpy as np

from arktika.pmc_vorticity import EARTH_RADIUS_M, derive_relative_vorticity_850


def passport(**updates):
    value = {
        "source": "era5", "field_id": "a" * 24, "input_sha256": "b" * 64,
        "variable": "wind", "units": "m s-1",
        "level_hPa": 850, "valid_time": "2024-01-01T00:00:00Z",
        "time_kind": "instantaneous_analysis", "wind_reference": "east_north",
        "source_version": "synthetic-test-source-v1",
    }
    value.update(updates)
    return value


class RelativeVorticityTests(unittest.TestCase):
    def axes(self):
        return np.linspace(60, 75, 31), np.linspace(0, 5, 21)

    def test_solid_body_rotation_matches_analytic_spherical_vorticity(self):
        lat, lon = self.axes()
        phi = np.deg2rad(lat)[:, None]
        omega = 2.0e-5
        u = np.broadcast_to(omega * EARTH_RADIUS_M * np.cos(phi), (lat.size, lon.size))
        v = np.zeros_like(u)
        result = derive_relative_vorticity_850(
            u, v, lat, lon, field_metadata=passport(), grid_id="synthetic-grid")
        expected = np.broadcast_to(2.0 * omega * np.sin(phi), u.shape)
        np.testing.assert_allclose(result["values"], expected, rtol=1.1e-4, atol=2e-11)
        self.assertEqual(result["passport"]["quantity"], "relative_vorticity")
        self.assertEqual(result["passport"]["grid"]["crs"], "EPSG:4326")
        self.assertEqual(result["passport"]["grid"]["grid_id"], "synthetic-grid")
        self.assertEqual(result["passport"]["valid_time"], "2024-01-01T00:00:00Z")
        self.assertEqual(result["passport"]["maturity"], "experimental")
        self.assertEqual(result["passport"]["claimed_source_file_sha256"], "b" * 64)
        self.assertEqual(len(result["passport"]["input_arrays_sha256"]), 64)

    def test_missing_wind_stencil_remains_missing(self):
        lat, lon = self.axes()
        u = np.ones((lat.size, lon.size))
        v = np.zeros_like(u)
        u[15, 10] = np.nan
        result = derive_relative_vorticity_850(u, v, lat, lon, field_metadata=passport())
        self.assertTrue(np.isnan(result["values"][15, 10]))
        self.assertTrue(np.isnan(result["values"][14, 10]))
        self.assertTrue(np.isfinite(result["values"][5, 5]))
        self.assertIn("NaN", result["passport"]["nodata"])

    def test_masked_wind_stencil_remains_missing(self):
        lat, lon = self.axes()
        u = np.ma.array(np.ones((lat.size, lon.size)), mask=False)
        v = np.zeros((lat.size, lon.size))
        u.mask[15, 10] = True
        result = derive_relative_vorticity_850(u, v, lat, lon, field_metadata=passport())
        self.assertTrue(np.isnan(result["values"][15, 10]))
        self.assertTrue(np.isnan(result["values"][14, 10]))

    def test_rejects_nonuniform_rectilinear_axes(self):
        lat, lon = self.axes()
        lon[10] += 0.1
        with self.assertRaisesRegex(ValueError, "uniform spacing"):
            derive_relative_vorticity_850(np.ones((lat.size, lon.size)),
                                          np.zeros((lat.size, lon.size)), lat, lon,
                                          field_metadata=passport())

    def test_accepts_dateline_crossing_local_window(self):
        lat = np.linspace(60, 70, 5)
        lon = np.array([178, 179, -180, -179, -178], dtype=float)
        u = np.ones((5, 5)); v = np.zeros((5, 5))
        result = derive_relative_vorticity_850(u, v, lat, lon, field_metadata=passport())
        self.assertEqual(result["values"].shape, (5, 5))
        self.assertTrue(np.isfinite(result["values"]).all())

    def test_rejects_curvilinear_grid(self):
        lat, lon = self.axes()
        lat2, lon2 = np.meshgrid(lat, lon, indexing="ij")
        lon2 = lon2 + 0.01 * np.sin(np.deg2rad(lat2)) * np.arange(lon.size)[None, :]
        with self.assertRaisesRegex(ValueError, "curvilinear"):
            derive_relative_vorticity_850(np.ones_like(lat2), np.zeros_like(lat2),
                                          lat2, lon2, field_metadata=passport())

    def test_rejects_unproven_vector_reference(self):
        lat, lon = self.axes()
        with self.assertRaisesRegex(ValueError, "reference"):
            derive_relative_vorticity_850(np.ones((31, 21)), np.zeros((31, 21)), lat, lon,
                                          field_metadata=passport(wind_reference="unknown"))

    def test_requires_immutable_input_field_digest(self):
        lat, lon = self.axes()
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            derive_relative_vorticity_850(
                np.ones((lat.size, lon.size)), np.zeros((lat.size, lon.size)), lat, lon,
                field_metadata=passport(input_sha256="not-a-digest"))

    def test_rejects_wrong_source_level_time_units_and_future_semantics(self):
        lat, lon = self.axes()
        u = np.ones((31, 21)); v = np.zeros_like(u)
        for changes in (
            {"source": "carra2"}, {"level_hPa": 700}, {"units": "knots"},
            {"time_kind": "hourly_mean"}, {"valid_time": "2024-01-01T00:00:00+03:00"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                derive_relative_vorticity_850(u, v, lat, lon,
                                              field_metadata=passport(**changes))

    def test_rejects_bad_grid_shape_axes_and_nonfinite_input(self):
        lat, lon = self.axes()
        with self.assertRaises(ValueError):
            derive_relative_vorticity_850(np.ones((31, 21)), np.zeros((31, 20)), lat, lon,
                                          field_metadata=passport())
        with self.assertRaises(ValueError):
            derive_relative_vorticity_850(np.ones((31, 21)), np.zeros((31, 21)), lat,
                                          np.array([0, 1, 2, 3, 4, 3] + list(np.arange(6, 21))),
                                          field_metadata=passport())
        u = np.ones((31, 21)); u[4, 4] = np.inf
        with self.assertRaises(ValueError):
            derive_relative_vorticity_850(u, np.zeros_like(u), lat, lon, field_metadata=passport())


if __name__ == "__main__":
    unittest.main()
