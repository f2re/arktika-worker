import unittest

from arktika.pmc_archive_run import build_archive_report, target_grid_for_area, validate_grid


GRID = {
    "crs": "EPSG:3413", "width": 20, "height": 20,
    "transform": [10_000, 0, -100_000, 0, -10_000, 100_000],
    "resolution_x_m": 10_000, "resolution_y_m": 10_000,
}


def frame(hour, source="era5", candidate_id="c"):
    stamp = f"2024-01-01T{hour:02d}:00:00Z"
    return {
        "source": source, "valid_time": stamp, "grid": dict(GRID),
        "candidate_count": 1,
        "candidates": [{"candidate_id": candidate_id,
                        "centroid_pixel_rc": [10.0, 10.0],
                        "classification": "candidate",
                        "confirmation_status": "not_confirmed"}],
    }


class PmcArchiveRunTests(unittest.TestCase):
    def test_target_grid_requires_bounded_projected_metre_affine(self):
        valid = validate_grid(GRID)
        self.assertEqual("EPSG:3413", valid["crs"])
        for invalid in (
            dict(GRID, width=4096, height=1024),
            dict(GRID, crs="EPSG:4326"),
            dict(GRID, transform=[0, 1]),
            {**GRID, "extra": "ignored?"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_grid(invalid)

    def test_target_grid_is_cropped_to_selected_geographic_region(self):
        full = target_grid_for_area(preset="arctic", width=768,
                                    area=[89, -180, 45, 180 - 1e-6])
        region = target_grid_for_area(preset="arctic", width=768,
                                      area=[80, -20, 60, 70])
        self.assertLess(region["width"] * region["height"], full["width"] * full["height"])
        self.assertEqual(region["resolution_x_m"], full["resolution_x_m"])

    def test_missing_and_failed_times_are_unknown_and_never_count_as_clear(self):
        expected = {"era5": [f"2024-01-01T{hour:02d}:00:00Z" for hour in range(3)]}
        records = [
            {"field_id": "a" * 24, "source": "era5", "valid_time": expected["era5"][0],
             "status": "completed", "result": frame(0)},
            {"field_id": "b" * 24, "source": "era5", "valid_time": expected["era5"][1],
             "status": "error", "error": "synthetic processing failure"},
        ]
        report = build_archive_report(
            task_id="t" * 32, plan={"sources": ["era5"], "start": expected["era5"][0],
                                     "end": expected["era5"][-1]}, expected_times=expected,
            interval_hours={"era5": 1}, frame_records=records,
            cached_fields=[{"source": "era5", "variable": "wind", "level": 850,
                            "time": expected["era5"][0]}],
            target_grid=GRID,
        )
        source = report["coverage"][0]
        self.assertEqual("partial", report["status"])
        self.assertEqual(3, source["expected_steps"])
        self.assertEqual(1, source["processed_steps"])
        self.assertEqual(2, source["unprocessed_steps"])
        self.assertEqual(expected["era5"][1:], source["unprocessed_preview_utc"])
        self.assertEqual("not_supplied", report["satellite_comparison"]["status"])
        self.assertNotIn("genesis_location", report["tracks_by_source"][0]["tracks"][0])
        self.assertEqual({"start": expected["era5"][0], "end": expected["era5"][-1]},
                         report["passport"]["valid_period"])
        self.assertEqual("candidate_track_catalog", report["passport"]["quantity_kind"])
        self.assertEqual("not_assessed", report["passport"]["uncertainty"]["status"])

    def test_tracks_are_split_at_missing_times_and_marked_left_censored(self):
        expected = {"era5": [f"2024-01-01T{hour:02d}:00:00Z" for hour in range(4)]}
        records = [
            {"field_id": "a" * 24, "source": "era5", "valid_time": expected["era5"][0],
             "status": "completed", "result": frame(0, candidate_id="first")},
            {"field_id": "d" * 24, "source": "era5", "valid_time": expected["era5"][3],
             "status": "completed", "result": frame(3, candidate_id="after-gap")},
        ]
        report = build_archive_report(
            task_id="t" * 32, plan={"sources": ["era5"]}, expected_times=expected,
            interval_hours={"era5": 1}, frame_records=records, cached_fields=[],
            target_grid=GRID,
        )
        tracks = report["tracks_by_source"][0]["tracks"]
        self.assertEqual(2, len(tracks))
        self.assertTrue(all(track["left_censored"] for track in tracks))
        self.assertEqual(2, report["coverage"][0]["unprocessed_steps"])

    def test_all_failed_archive_does_not_claim_a_product_passport(self):
        expected = {"era5": ["2024-01-01T00:00:00Z"]}
        report = build_archive_report(
            task_id="t" * 32,
            plan={"sources": ["era5"], "start": expected["era5"][0],
                  "end": expected["era5"][0]},
            expected_times=expected,
            interval_hours={"era5": 1},
            frame_records=[{"field_id": "a" * 24, "source": "era5",
                            "valid_time": expected["era5"][0], "status": "error",
                            "error": "synthetic failure"}],
            cached_fields=[], target_grid=GRID,
        )
        self.assertNotIn("passport", report)

    def test_satellite_comparisons_require_exact_utc_and_target_grid_and_remain_pixel_only(self):
        times = [f"2024-01-01T{hour:02d}:00:00Z" for hour in range(3)]
        other_grid = dict(GRID, transform=[10_000, 0, -90_000, 0, -10_000, 100_000])

        def comparison(valid_time, grid):
            return {
                "source": "era5",
                "valid_time": times[0],
                "grid": dict(GRID),
                "comparison": {
                    "status": "comparison_only",
                    "observation": {"source": "manual_satellite", "product_id": "labels-v1",
                                     "valid_time": valid_time, "observation_id": "o" * 24,
                                     "grid": dict(grid)},
                    "metrics": {"true_positive_pixels": 2, "false_positive_pixels": 1,
                                "false_negative_pixels": 1, "true_negative_pixels": 16,
                                "intersection_over_union": .5,
                                "probability_of_detection": .6666666667,
                                "false_alarm_ratio": .3333333333},
                    "coverage": {"observed_pixels": 20, "unknown_pixels": 0},
                },
            }

        records = [{"field_id": f"{hour + 1:x}" * 24, "source": "era5", "valid_time": times[hour],
                    "status": "completed", "result": frame(hour)} for hour in range(3)]
        wrong_utc = comparison(times[1], GRID)
        wrong_utc["valid_time"] = times[1]
        wrong_utc["comparison"]["observation"]["valid_time"] = times[0]
        report = build_archive_report(
            task_id="t" * 32, plan={"sources": ["era5"]},
            expected_times={"era5": times}, interval_hours={"era5": 1},
            frame_records=records, cached_fields=[], target_grid=GRID,
            satellite_comparisons=[
                comparison(times[0], GRID),
                wrong_utc,
                {**comparison(times[2], other_grid), "valid_time": times[2]},
            ],
        )
        satellite = report["satellite_comparison"]
        self.assertEqual("partial_comparison", satellite["status"])
        self.assertEqual(1, satellite["matched_frame_count"])
        self.assertEqual(2, satellite["unknown_frame_count"])
        self.assertEqual(2, satellite["metrics"]["true_positive_pixels"])
        self.assertEqual("not_meteorological_validation", satellite["validation_status"])
        self.assertIn("пиксельное", satellite["comparison_basis"])
        self.assertIn("не означает отсутствие", satellite["unknown_means"])
        self.assertEqual(3, len(satellite["frames"]))
        self.assertEqual(["comparison_only", "unknown_unmatched", "unknown_unmatched"],
                         [row["status"] for row in satellite["frames"]])

    def test_missing_satellite_comparison_stays_unknown(self):
        times = ["2024-01-01T00:00:00Z"]
        report = build_archive_report(
            task_id="t" * 32, plan={"sources": ["era5"]},
            expected_times={"era5": times}, interval_hours={"era5": 1},
            frame_records=[{"field_id": "a" * 24, "source": "era5", "valid_time": times[0],
                            "status": "completed", "result": frame(0)}],
            cached_fields=[], target_grid=GRID,
        )
        satellite = report["satellite_comparison"]
        self.assertEqual("not_supplied", satellite["status"])
        self.assertIsNone(satellite["metrics"])
        self.assertEqual(1, satellite["unknown_frame_count"])


if __name__ == "__main__":
    unittest.main()
