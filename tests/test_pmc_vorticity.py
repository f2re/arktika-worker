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
        self.assertEqual("1.0", result["passport"]["schema_version"])
        self.assertEqual("atmospheric_dynamics", result["passport"]["quantity_kind"])
        self.assertEqual("not_applicable", result["passport"]["calibration"]["status"])
        self.assertEqual("era5", result["passport"]["sources"][0]["source_id"])
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

    def test_curvilinear_mesh_recovers_solid_body_rotation(self):
        lat_axis = np.linspace(60, 75, 61)
        lon_axis = np.linspace(0, 4, 41)
        lat, lon = np.meshgrid(np.deg2rad(lat_axis), np.deg2rad(lon_axis), indexing="ij")
        # Smoothly warp both coordinates without changing the physical flow.
        phi = lat + 0.012 * np.sin(2.0 * lon) * np.cos(lat)
        lam = lon + 0.015 * np.sin(phi - phi[0, 0])
        omega = 2.0e-5
        u = omega * EARTH_RADIUS_M * np.cos(phi)
        result = derive_relative_vorticity_850(
            u, np.zeros_like(u), np.rad2deg(phi), np.rad2deg(lam),
            field_metadata=passport(source="carra2", wind_reference="geographic_from_direction"),
            grid_id="curvilinear-test-grid")
        expected = 2.0 * omega * np.sin(phi)
        np.testing.assert_allclose(result["values"][2:-2, 2:-2],
                                   expected[2:-2, 2:-2], rtol=2e-4, atol=2e-10)
        self.assertEqual(result["passport"]["grid"]["grid_type"],
                         "curvilinear_latitude_longitude")
        self.assertEqual(result["passport"]["algorithm_version"], "0.2.0")

    def test_curvilinear_mesh_recovers_longitudinal_derivative(self):
        lat_axis = np.linspace(60, 75, 31)
        lon_axis = np.linspace(0, 3, 25)
        lat, lon = np.meshgrid(np.deg2rad(lat_axis), np.deg2rad(lon_axis), indexing="ij")
        phi = lat + 0.01 * np.sin(lon)
        lam = lon + 0.01 * np.sin(lat)
        coefficient = 7.0
        result = derive_relative_vorticity_850(
            np.zeros_like(phi), coefficient * lam, np.rad2deg(phi), np.rad2deg(lam),
            field_metadata=passport(source="carra2", wind_reference="geographic_from_direction"))
        expected = coefficient / (EARTH_RADIUS_M * np.cos(phi))
        np.testing.assert_allclose(result["values"][2:-2, 2:-2],
                                   expected[2:-2, 2:-2], rtol=5e-4, atol=2e-12)

    def test_curvilinear_uniform_eastward_wind_includes_spherical_metric_term(self):
        lat_axis = np.linspace(60, 75, 31)
        lon_axis = np.linspace(0, 3, 25)
        lat, lon = np.meshgrid(np.deg2rad(lat_axis), np.deg2rad(lon_axis), indexing="ij")
        phi = lat + 0.008 * np.sin(lon)
        lam = lon + 0.008 * np.sin(lat)
        speed = 12.0
        result = derive_relative_vorticity_850(
            np.full_like(phi, speed), np.zeros_like(phi), np.rad2deg(phi), np.rad2deg(lam),
            field_metadata=passport(source="carra2", wind_reference="geographic_from_direction"))
        expected = speed * np.tan(phi) / EARTH_RADIUS_M
        np.testing.assert_allclose(result["values"][2:-2, 2:-2],
                                   expected[2:-2, 2:-2], rtol=2e-4, atol=2e-12)

    def test_curvilinear_mesh_unwraps_dateline_consistently(self):
        phi_axis = np.deg2rad(np.linspace(65, 70, 11))
        lam_axis = np.deg2rad(np.linspace(178, 182, 13))
        phi, lam = np.meshgrid(phi_axis, lam_axis, indexing="ij")
        phi = phi + 0.002 * np.sin(lam - lam[0, 0])
        lam = lam + 0.003 * np.sin(phi - phi[0, 0])
        lon_degrees = ((np.rad2deg(lam) + 180.0) % 360.0) - 180.0
        speed = 8.0
        result = derive_relative_vorticity_850(
            np.full_like(phi, speed), np.zeros_like(phi), np.rad2deg(phi), lon_degrees,
            field_metadata=passport(source="carra2", wind_reference="geographic_from_direction"))
        expected = speed * np.tan(phi) / EARTH_RADIUS_M
        np.testing.assert_allclose(result["values"][2:-2, 2:-2],
                                   expected[2:-2, 2:-2], rtol=1e-3, atol=2e-12)

    def test_rejects_folded_curvilinear_mesh(self):
        lat_axis = np.linspace(60, 70, 9)
        lon_axis = np.linspace(0, 8, 9)
        lat, lon = np.meshgrid(lat_axis, lon_axis, indexing="ij")
        lon[:, 5:] = lon[:, 5:][:, ::-1]
        with self.assertRaisesRegex(ValueError, "Jacobian|monotonic"):
            derive_relative_vorticity_850(np.ones_like(lat), np.zeros_like(lat), lat, lon,
                                          field_metadata=passport(source="carra2"))

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
            {"source": "other"}, {"level_hPa": 700}, {"units": "knots"},
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
