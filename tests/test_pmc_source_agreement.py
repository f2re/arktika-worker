import unittest

from arktika.pmc_source_agreement import compare_candidate_sources


GRID = {"crs": "EPSG:3413", "width": 100, "height": 100,
        "resolution_x_m": 10_000, "resolution_y_m": 10_000,
        "transform": [10_000, 0, -500_000, 0, -10_000, 500_000]}


def candidate(identifier, row, col):
    return {"candidate_id": identifier, "centroid_pixel_rc": [row, col]}


def frame(source, hour, candidates, grid=None):
    return {"source": source, "valid_time": f"2025-01-02T{hour:02d}:00:00Z",
            "grid": dict(GRID if grid is None else grid), "candidates": candidates}


class PmcSourceAgreementTests(unittest.TestCase):
    def test_exact_common_time_matches_unambiguous_candidates_and_own_denominators(self):
        result = compare_candidate_sources([
            frame("ERA5", 0, [candidate("e1", 10, 10), candidate("e2", 50, 50)]),
            frame("MERRA2", 0, [candidate("m1", 10, 11)]),
        ], expected_sources=["ERA5", "MERRA2"], max_distance_m=20_000)
        self.assertEqual("experimental", result["maturity"])
        self.assertEqual("not_provided", result["genesis_assessment"])
        self.assertEqual("comparable", result["frames"][0]["status"])
        summary = result["pairwise_summary"]["ERA5|MERRA2"]
        self.assertEqual({"ERA5": 2, "MERRA2": 1}, summary["candidate_count_by_source"])
        self.assertEqual({"ERA5": 1, "MERRA2": 1}, summary["matched_candidate_count_by_source"])
        self.assertEqual(0.5, summary["match_fraction_by_source"]["ERA5"])
        self.assertEqual(1.0, summary["match_fraction_by_source"]["MERRA2"])

    def test_many_to_many_edges_are_unresolved_and_not_counted(self):
        result = compare_candidate_sources([
            frame("A", 0, [candidate("a1", 10, 10), candidate("a2", 10, 11)]),
            frame("B", 0, [candidate("b1", 10, 10), candidate("b2", 10, 11)]),
        ], expected_sources=["A", "B"], max_distance_m=20_000)
        pair = result["frames"][0]["pairs"][0]
        self.assertEqual([], pair["matches"])
        self.assertGreater(pair["unresolved_ambiguous_edges"], 0)
        self.assertEqual(0, result["pairwise_summary"]["A|B"]["matched_candidate_count_by_source"]["A"])

    def test_missing_source_time_is_unknown_not_a_negative_detection(self):
        result = compare_candidate_sources([
            frame("A", 0, []), frame("A", 1, [candidate("a", 1, 1)]),
            frame("B", 0, []),
        ], expected_sources=["A", "B"])
        self.assertEqual("comparable", result["frames"][0]["status"])
        self.assertEqual("unknown_missing_source_or_timestamp", result["frames"][1]["status"])
        self.assertEqual(["B"], result["frames"][1]["missing_sources"])
        self.assertEqual(1, result["source_coverage"]["B"]["missing_frames"])
        self.assertEqual(1, result["pairwise_summary"]["A|B"]["comparable_frames"])

    def test_grid_mismatch_is_uncomparable_even_when_times_match(self):
        moved = dict(GRID, transform=[10_000, 0, -490_000, 0, -10_000, 500_000])
        result = compare_candidate_sources([frame("A", 0, [], GRID), frame("B", 0, [], moved)],
                                          expected_sources=["A", "B"])
        self.assertEqual("uncomparable_grid_mismatch", result["frames"][0]["status"])
        self.assertEqual(0, result["pairwise_summary"]["A|B"]["comparable_frames"])

    def test_time_must_be_exact_utc_and_grid_projected_in_metres(self):
        bad_time = frame("A", 0, [])
        bad_time["valid_time"] = "2025-01-02T01:00:00+01:00"
        with self.assertRaisesRegex(ValueError, "UTC"):
            compare_candidate_sources([bad_time], expected_sources=["A"])
        bad_grid = dict(GRID, crs="EPSG:4326")
        with self.assertRaisesRegex(ValueError, "projected metre"):
            compare_candidate_sources([frame("A", 0, [], bad_grid)], expected_sources=["A"])

    def test_carra2_era5_dependency_is_explicit(self):
        result = compare_candidate_sources([], expected_sources=["ERA5", "CARRA2"])
        self.assertTrue(any("CARRA2" in text and "ERA5" in text for text in result["limitations"]))

    def test_planned_times_missing_for_every_source_remain_unknown(self):
        schedules = {
            "A": ["2025-01-02T00:00:00Z", "2025-01-02T01:00:00Z"],
            "B": ["2025-01-02T00:00:00Z", "2025-01-02T01:00:00Z"],
        }
        result = compare_candidate_sources([
            frame("A", 0, []), frame("B", 0, []),
        ], expected_sources=["A", "B"], expected_times=schedules)
        self.assertEqual("unknown_missing_source_or_timestamp", result["frames"][1]["status"])
        self.assertEqual(2, result["source_coverage"]["A"]["expected_frames"])
        self.assertEqual(1, result["source_coverage"]["A"]["missing_frames"])
        self.assertEqual(1, result["source_coverage"]["B"]["missing_frames"])

    def test_different_source_cadences_compare_only_scheduled_overlap(self):
        result = compare_candidate_sources([
            frame("A", 0, []), frame("A", 1, []), frame("B", 0, []),
        ], expected_sources=["A", "B"], expected_times={
            "A": ["2025-01-02T00:00:00Z", "2025-01-02T01:00:00Z"],
            "B": ["2025-01-02T00:00:00Z"],
        })
        self.assertEqual("comparable", result["frames"][0]["status"])
        self.assertEqual("not_common_expected_time", result["frames"][1]["status"])
        self.assertEqual(1, result["pairwise_summary"]["A|B"]["comparable_frames"])


if __name__ == "__main__":
    unittest.main()
