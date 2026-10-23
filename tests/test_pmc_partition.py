import unittest

import numpy as np

from arktika.pmc_partition import partition_component


GRID = {"transform": [1_000, 0, 0, 0, -1_000, 5_000]}


class PmcPartitionTests(unittest.TestCase):
    def fixture(self):
        field = np.ones((3, 9), dtype=float)
        field[1, 1] = 10.0
        field[1, 2:6] = 4.0
        field[1, 5] = 6.0
        field[1, 6] = 9.0
        component = np.ones_like(field, dtype=bool)
        peaks = [
            {"peak_vorticity_s-1": 10.0, "peak_pixel_rc": [1, 1],
             "peak_seed_pixel_rc": [1, 1], "saddle_vorticity_s-1": 1.0,
             "relative_isolation_contrast": 0.9,
             "isolated_by_experimental_gamma": True},
            {"peak_vorticity_s-1": 9.0, "peak_pixel_rc": [1, 6],
             "peak_seed_pixel_rc": [1, 6], "saddle_vorticity_s-1": 4.0,
             "relative_isolation_contrast": 5 / 9,
             "isolated_by_experimental_gamma": True},
        ]
        return field, component, peaks

    def test_two_isolated_peaks_partition_component_and_conserve_coverage(self):
        field, component, peaks = self.fixture()
        result = partition_component(field, component, peaks, threshold=1.0,
                                     grid=GRID, boundary_censored=False)
        self.assertEqual("partitioned_experimental", result["status"])
        self.assertTrue(result["coverage_conserved"])
        self.assertTrue(np.array_equal(result["labels"] > 0, component))
        self.assertEqual({1, 2}, set(np.unique(result["labels"])))
        self.assertEqual(int(component.sum()), sum(region["pixel_count"]
                                                   for region in result["regions"]))
        self.assertTrue(all(region["classification"] == "candidate"
                            and region["confirmation_status"] == "not_confirmed"
                            for region in result["regions"]))

    def test_one_peak_expands_to_full_component(self):
        field, component, peaks = self.fixture()
        result = partition_component(field, component, peaks[:1], threshold=1.0,
                                     grid=GRID, boundary_censored=False)
        self.assertEqual("single_peak_expanded", result["status"])
        self.assertTrue(np.array_equal(result["labels"] > 0, component))
        self.assertEqual(int(component.sum()), result["regions"][0]["pixel_count"])

    def test_no_isolated_peak_keeps_one_unpartitioned_candidate(self):
        field, component, peaks = self.fixture()
        for peak in peaks:
            peak["isolated_by_experimental_gamma"] = False
        result = partition_component(field, component, peaks, threshold=1.0,
                                     grid=GRID, boundary_censored=False)
        self.assertEqual("no_isolated_peak_unpartitioned", result["status"])
        self.assertEqual(1, len(result["regions"]))
        self.assertTrue(np.array_equal(result["labels"] > 0, component))

    def test_boundary_censored_component_is_not_split(self):
        field, component, peaks = self.fixture()
        result = partition_component(field, component, peaks, threshold=1.0,
                                     grid=GRID, boundary_censored=True)
        self.assertEqual("boundary_censored_unpartitioned", result["status"])
        self.assertEqual(1, len(result["regions"]))
        self.assertTrue(np.array_equal(result["labels"] > 0, component))

    def test_result_is_deterministic_for_ties(self):
        field, component, peaks = self.fixture()
        first = partition_component(field, component, peaks, threshold=1.0,
                                    grid=GRID, boundary_censored=False)
        second = partition_component(field, component, peaks, threshold=1.0,
                                     grid=GRID, boundary_censored=False)
        self.assertTrue(np.array_equal(first["labels"], second["labels"]))

    def test_contour_core_keeps_cells_that_nearest_seed_would_reassign(self):
        field = np.array([[10e-6, 10e-6, 4e-6, 4e-6, 4e-6,
                           7e-6, 7e-6, 7e-6, 8e-6, 8e-6, 8e-6, 8e-6]])
        component = np.ones_like(field, dtype=bool)
        peaks = [
            {"peak_vorticity_s-1": 10e-6, "peak_pixel_rc": [0.0, 0.5],
             "peak_seed_pixel_rc": [0, 0], "saddle_vorticity_s-1": 4e-6,
             "relative_isolation_contrast": 0.6, "isolated_by_experimental_gamma": True},
            {"peak_vorticity_s-1": 8e-6, "peak_pixel_rc": [0.0, 9.5],
             "peak_seed_pixel_rc": [0, 11], "saddle_vorticity_s-1": 4e-6,
             "relative_isolation_contrast": 0.5, "isolated_by_experimental_gamma": True},
        ]
        result = partition_component(
            field, component, peaks, threshold=3e-6,
            grid={"transform": [1, 0, 0, 0, -1, 0]}, boundary_censored=False)
        # Col 5 lies inside peak 2's contour core, although seed 1 is nearer.
        self.assertEqual(2, int(result["labels"][0, 5]))


if __name__ == "__main__":
    unittest.main()
