import unittest

import numpy as np

from arktika.pmc_detection import (
    ALGORITHM_VERSION,
    _smooth_uniform_radius,
    detect_candidates,
)


class PmcDetectionTests(unittest.TestCase):
    def setUp(self):
        self.grid = {
            "crs": "EPSG:3413",
            "width": 15,
            "height": 15,
            "resolution_x_m": 60000,
            "resolution_y_m": 60000,
            "transform": [60000, 0, -450000, 0, -60000, 450000],
        }

    def field(self):
        values = np.zeros((15, 15), dtype=float)
        values[4:11, 4:11] = 2.0e-4
        return values

    def run_detector(self, **kwargs):
        return detect_candidates(
            self.field(), grid=self.grid, valid_time="2025-01-02T06:00:00Z",
            source="ERA5: test fixture", source_field_metadata={
                "source": "ERA5: test fixture", "variable": "relative_vorticity",
                "level_hPa": 850, "units": "s-1", "valid_time": "2025-01-02T06:00:00Z",
                "grid": dict(self.grid),
            }, **kwargs
        )

    def test_uniform_radius_filter_uses_circular_metric_footprint_and_renormalizes_nodata(self):
        values = np.full((5, 5), np.nan)
        values[2, 2] = 5.0
        smoothed = _smooth_uniform_radius(values, radius_km=60.0,
                                          resolution_x_m=60000, resolution_y_m=60000)
        # At this deliberately coarse synthetic grid only the center cell is valid;
        # normalized convolution must preserve it rather than dilute it with nodata.
        self.assertEqual(5.0, smoothed[2, 2])

        values = np.zeros((5, 5), dtype=float)
        values[2, 2] = 5.0
        smoothed = _smooth_uniform_radius(values, radius_km=60.0,
                                          resolution_x_m=60000, resolution_y_m=60000)
        self.assertAlmostEqual(1.0, smoothed[2, 2])
        self.assertEqual(0.0, smoothed[1, 1])  # diagonal lies outside the 60-km disk

    def test_detects_only_unconfirmed_experimental_candidates(self):
        result = self.run_detector()
        self.assertEqual(ALGORITHM_VERSION, result["algorithm_version"])
        self.assertEqual("experimental", result["maturity"])
        self.assertGreater(result["candidate_count"], 0)
        self.assertTrue(all(c["classification"] == "candidate" for c in result["candidates"]))
        self.assertTrue(all(c["confirmation_status"] == "not_confirmed" for c in result["candidates"]))
        self.assertEqual("not_supplied", result["satellite_comparison"]["status"])
        self.assertIsNone(result["satellite_comparison"]["metrics"])

    def test_satellite_metrics_only_when_observation_mask_provided(self):
        observations = np.zeros((15, 15), dtype=bool)
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.run_detector(satellite_observation_mask=observations)
        result = self.run_detector(
            satellite_observation_mask=observations,
            satellite_observation_metadata={
                "source": "fixture satellite archive",
                "product_id": "test-visible-mask",
                "valid_time": "2025-01-02T06:00:00Z",
                "observation_id": "fixture-001",
                "grid": dict(self.grid),
            },
        )
        comparison = result["satellite_comparison"]
        self.assertEqual("comparison_only", comparison["status"])
        self.assertEqual(0, comparison["metrics"]["true_positive_pixels"])
        self.assertGreater(comparison["metrics"]["false_positive_pixels"], 0)
        self.assertEqual(0.0, comparison["metrics"]["intersection_over_union"])

    def test_rejects_missing_or_inconsistent_scientific_metadata(self):
        with self.assertRaisesRegex(ValueError, "grid requires"):
            detect_candidates(self.field(), grid={"crs": "EPSG:3413"},
                              valid_time="2025-01-02T06:00:00Z", source="ERA5",source_field_metadata={})
        bad_grid = dict(self.grid, width=14)
        with self.assertRaisesRegex(ValueError, "dimensions"):
            detect_candidates(self.field(), grid=bad_grid,
                              valid_time="2025-01-02T06:00:00Z", source="ERA5",source_field_metadata={})
        with self.assertRaisesRegex(ValueError, "projected metric"):
            detect_candidates(self.field(), grid=dict(self.grid, crs="EPSG:4326"),
                              valid_time="2025-01-02T06:00:00Z", source="ERA5",source_field_metadata={})
        with self.assertRaisesRegex(ValueError, "UTC"):
            detect_candidates(self.field(), grid=self.grid,
                              valid_time="2025-01-02T06:00:00", source="ERA5",source_field_metadata={})
        with self.assertRaisesRegex(ValueError, "provenance"):
            detect_candidates(self.field(), grid=self.grid,
                              valid_time="2025-01-02T06:00:00Z", source=" ",source_field_metadata={})

    def test_rejects_satellite_mask_on_different_grid(self):
        with self.assertRaisesRegex(ValueError, "same-grid"):
            self.run_detector(
                satellite_observation_mask=np.zeros((2, 2), dtype=bool),
                satellite_observation_metadata={
                    "source": "fixture", "product_id": "mask",
                    "valid_time": "2025-01-02T06:00:00Z", "observation_id": "one",
                    "grid": dict(self.grid),
                },
            )

    def test_rejects_satellite_grid_with_same_shape_but_shifted_origin(self):
        observations = np.zeros((15, 15), dtype=bool)
        shifted = dict(self.grid, transform=[60000, 0, -390000, 0, -60000, 450000])
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self.run_detector(
                satellite_observation_mask=observations,
                satellite_observation_metadata={
                    "source": "fixture", "product_id": "mask",
                    "valid_time": "2025-01-02T06:00:00Z", "observation_id": "one",
                    "grid": shifted,
                },
            )

    def test_rejects_source_field_with_wrong_level_or_units(self):
        field = {
            "source": "ERA5", "variable": "relative_vorticity", "level_hPa": 700,
            "units": "s-1", "valid_time": "2025-01-02T06:00:00Z", "grid": dict(self.grid),
        }
        with self.assertRaisesRegex(ValueError, "source field passport"):
            detect_candidates(self.field(), grid=self.grid,
                              valid_time="2025-01-02T06:00:00Z", source="ERA5",
                              source_field_metadata=field)


if __name__ == "__main__":
    unittest.main()
