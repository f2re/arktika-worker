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
        self.assertEqual({"start": expected["era5"][0], "end": expected["era5"][0]},
                         report["passport"]["valid_period"])
        self.assertEqual(expected["era5"][0], report["plan"]["start"])
        self.assertEqual(expected["era5"][-1], report["plan"]["end"])
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

    def test_candidate_emergence_ledger_distinguishes_empty_screen_from_genesis(self):
        times = [f"2024-01-01T{hour:02d}:00:00Z" for hour in range(3)]
        empty = frame(1)
        empty["candidates"] = []
        empty["candidate_count"] = 0
        records = [
            {"field_id": "a" * 24, "source": "era5", "valid_time": times[0],
             "status": "completed", "result": frame(0, candidate_id="first")},
            {"field_id": "b" * 24, "source": "era5", "valid_time": times[1],
             "status": "completed", "result": empty},
            {"field_id": "c" * 24, "source": "era5", "valid_time": times[2],
             "status": "completed", "result": frame(2, candidate_id="later")},
        ]
        report = build_archive_report(
            task_id="t" * 32, plan={"sources": ["era5"]},
            expected_times={"era5": times}, interval_hours={"era5": 1},
            frame_records=records, cached_fields=[], target_grid=GRID,
        )
        evidence = report["tracks_by_source"][0]["candidate_emergence_evidence"]
        self.assertEqual("left_censored_at_archive_window_start",
                         evidence[0]["prior_frame"]["status"])
        self.assertEqual("previous_processed_frame_had_no_model_candidates",
                         evidence[1]["prior_frame"]["status"])
        self.assertEqual(times[1], evidence[1]["prior_frame"]["valid_time"])
        self.assertEqual("not_assessed", evidence[1]["genesis_assessment"])
        self.assertIn("не является физическим зарождением", evidence[1]["interpretation"])

    def test_candidate_emergence_with_missing_previous_frame_is_unknown(self):
        times = [f"2024-01-01T{hour:02d}:00:00Z" for hour in range(4)]
        records = [
            {"field_id": "a" * 24, "source": "era5", "valid_time": times[0],
             "status": "completed", "result": frame(0, candidate_id="before")},
            {"field_id": "d" * 24, "source": "era5", "valid_time": times[3],
             "status": "completed", "result": frame(3, candidate_id="after")},
        ]
        report = build_archive_report(
            task_id="t" * 32, plan={"sources": ["era5"]},
            expected_times={"era5": times}, interval_hours={"era5": 1},
            frame_records=records, cached_fields=[], target_grid=GRID,
        )
        evidence = report["tracks_by_source"][0]["candidate_emergence_evidence"]
        after = next(item for item in evidence if item["candidate_id"] == "after")
        self.assertEqual("unknown_previous_expected_frame_missing",
                         after["prior_frame"]["status"])
        self.assertEqual(times[2], after["prior_frame"]["valid_time"])
        self.assertEqual("not_assessed", after["genesis_assessment"])

    def test_candidate_emergence_with_conflicting_previous_records_is_unknown(self):
        times = [f"2024-01-01T{hour:02d}:00:00Z" for hour in range(2)]
        records = [
            {"field_id": "a" * 24, "source": "era5", "valid_time": times[0],
             "status": "completed", "result": frame(0)},
            {"field_id": "b" * 24, "source": "era5", "valid_time": times[0],
             "status": "error", "error": "synthetic processing failure"},
            {"field_id": "c" * 24, "source": "era5", "valid_time": times[1],
             "status": "completed", "result": frame(1, candidate_id="later")},
        ]
        records[0]["result"]["candidates"][0]["centroid_pixel_rc"] = [0.0, 0.0]
        records[-1]["result"]["candidates"][0]["centroid_pixel_rc"] = [19.0, 19.0]
        report = build_archive_report(
            task_id="t" * 32, plan={"sources": ["era5"]},
            expected_times={"era5": times}, interval_hours={"era5": 1},
            frame_records=records, cached_fields=[], target_grid=GRID,
        )
        evidence = report["tracks_by_source"][0]["candidate_emergence_evidence"]
        later = next(item for item in evidence if item["candidate_id"] == "later")
        self.assertEqual("unknown_ambiguous_previous_expected_frame",
                         later["prior_frame"]["status"])
        self.assertIsNone(later["prior_frame"]["candidate_count"])

    def test_candidate_emergence_with_invalid_previous_record_is_unknown(self):
        times = [f"2024-01-01T{hour:02d}:00:00Z" for hour in range(2)]
        records = [
            {"field_id": "a" * 24, "source": "era5", "valid_time": times[0],
             "status": "processing"},
            {"field_id": "b" * 24, "source": "era5", "valid_time": times[1],
             "status": "completed", "result": frame(1, candidate_id="later")},
        ]
        report = build_archive_report(
            task_id="t" * 32, plan={"sources": ["era5"]},
            expected_times={"era5": times}, interval_hours={"era5": 1},
            frame_records=records, cached_fields=[], target_grid=GRID,
        )
        evidence = report["tracks_by_source"][0]["candidate_emergence_evidence"]
        self.assertEqual("unknown_invalid_previous_expected_frame_record",
                         evidence[0]["prior_frame"]["status"])

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
        self.assertEqual("not_publishable", report["output_status"])
        self.assertEqual("not_publishable", report["publication"]["status"])

    def test_successful_frames_supply_honest_fallback_source_grid_and_period(self):
        records = [
            {"field_id": "a" * 24, "source": "era5",
             "valid_time": "2024-01-01T00:00:00Z", "status": "completed",
             "result": frame(0)},
            {"field_id": "b" * 24, "source": "era5",
             "valid_time": "2024-01-01T02:00:00Z", "status": "completed",
             "result": frame(2)},
        ]
        report = build_archive_report(
            task_id="t" * 32, plan={"sources": ["era5"]},
            expected_times={}, interval_hours={"era5": 1},
            frame_records=records, cached_fields=[], target_grid=GRID,
        )
        passport = report["passport"]
        self.assertEqual({"start": "2024-01-01T00:00:00Z",
                          "end": "2024-01-01T02:00:00Z"}, passport["valid_period"])
        self.assertEqual([{"source_id": "era5"}], passport["sources"])
        self.assertEqual("candidate_detection_grid", passport["source_grids"][0]["role"])
        self.assertEqual("candidate_only", report["output_status"])
        self.assertNotIn("publication", report)
        evidence = report["tracks_by_source"][0]["candidate_emergence_evidence"]
        self.assertIsNone(evidence[0]["onset_in_expected_schedule"])
        self.assertEqual("unknown_expected_schedule_unavailable",
                         evidence[0]["prior_frame"]["status"])

    def test_no_frames_is_explicitly_not_publishable_even_with_requested_grid_and_period(self):
        report = build_archive_report(
            task_id="t" * 32,
            plan={"sources": ["era5"], "start": "2024-01-01T00:00:00Z",
                  "end": "2024-01-01T00:00:00Z"},
            expected_times={"era5": ["2024-01-01T00:00:00Z"]},
            interval_hours={"era5": 1}, frame_records=[], cached_fields=[],
            target_grid=GRID,
        )
        self.assertNotIn("passport", report)
        self.assertEqual("not_publishable", report["output_status"])
        self.assertEqual("not_publishable", report["publication"]["status"])

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

    def test_cross_source_agreement_is_explicitly_candidate_only(self):
        records = [
            {"field_id": "a" * 24, "source": "era5", "valid_time": "2024-01-01T00:00:00Z",
             "status": "completed", "result": frame(0, "era5", "e1")},
            {"field_id": "b" * 24, "source": "merra2", "valid_time": "2024-01-01T00:00:00Z",
             "status": "completed", "result": frame(0, "merra2", "m1")},
        ]
        report = build_archive_report(
            task_id="t" * 32, plan={"sources": ["era5", "merra2"]},
            expected_times={"era5": ["2024-01-01T00:00:00Z"],
                            "merra2": ["2024-01-01T00:00:00Z"]},
            interval_hours={"era5": 1, "merra2": 1}, frame_records=records,
            cached_fields=[], target_grid=GRID,
        )
        agreement = report["cross_source_candidate_agreement"]
        self.assertEqual("experimental", agreement["maturity"])
        self.assertEqual("not_provided", agreement["genesis_assessment"])
        self.assertEqual("comparable", agreement["frames"][0]["status"])
        self.assertEqual(1, len(agreement["frames"][0]["pairs"][0]["matches"]))

    def test_candidate_emergence_links_exact_time_cross_source_and_satellite_evidence(self):
        stamp = "2024-01-01T01:00:00Z"
        records = [
            {"field_id": "a" * 24, "source": "era5", "valid_time": stamp,
             "status": "completed", "result": frame(1, "era5", "e1")},
            {"field_id": "b" * 24, "source": "merra2", "valid_time": stamp,
             "status": "completed", "result": frame(1, "merra2", "m1")},
        ]
        comparison = {
            "status": "comparison_only",
            "observation": {"source": "manual_satellite", "product_id": "mask-v1",
                            "observation_id": "o" * 24, "valid_time": stamp,
                            "grid": dict(GRID)},
            "metrics": {"true_positive_pixels": 1, "false_positive_pixels": 0,
                        "false_negative_pixels": 0, "true_negative_pixels": 399},
            "coverage": {"observed_pixels": 400, "unknown_pixels": 0},
        }
        report = build_archive_report(
            task_id="t" * 32, plan={"sources": ["era5", "merra2"]},
            expected_times={"era5": [stamp], "merra2": [stamp]},
            interval_hours={"era5": 1, "merra2": 1}, frame_records=records,
            cached_fields=[], target_grid=GRID,
            satellite_comparisons=[{"source": "era5", "valid_time": stamp,
                                    "grid": dict(GRID), "comparison": comparison}],
        )
        evidence = report["tracks_by_source"][0]["candidate_emergence_evidence"][0]
        cross = evidence["cross_source_evidence_at_first_detection"]
        self.assertEqual("compared_exact_utc", cross["status"])
        self.assertEqual("merra2", cross["pairs"][0]["other_source"])
        self.assertEqual("matched_candidate", cross["pairs"][0]["status"])
        satellite = evidence["satellite_evidence_at_first_detection"]
        self.assertEqual("comparison_only", satellite["status"])
        self.assertEqual("o" * 24, satellite["observation"]["observation_id"])
        self.assertEqual(400, satellite["coverage"]["observed_pixels"])
        self.assertEqual("not_assessed", evidence["genesis_assessment"])

    def test_candidate_emergence_does_not_use_nearest_cross_source_time(self):
        onset = "2024-01-01T01:00:00Z"
        other = "2024-01-01T02:00:00Z"
        records = [
            {"field_id": "a" * 24, "source": "era5", "valid_time": onset,
             "status": "completed", "result": frame(1, "era5", "e1")},
            {"field_id": "b" * 24, "source": "merra2", "valid_time": other,
             "status": "completed", "result": frame(2, "merra2", "m1")},
        ]
        report = build_archive_report(
            task_id="t" * 32, plan={"sources": ["era5", "merra2"]},
            expected_times={"era5": [onset], "merra2": [other]},
            interval_hours={"era5": 1, "merra2": 1}, frame_records=records,
            cached_fields=[], target_grid=GRID,
        )
        evidence = report["tracks_by_source"][0]["candidate_emergence_evidence"][0]
        self.assertEqual("not_common_expected_time",
                         evidence["cross_source_evidence_at_first_detection"]["status"])
        self.assertEqual("unknown_not_compared",
                         evidence["satellite_evidence_at_first_detection"]["status"])

    def test_satellite_reprojection_failure_reason_is_reported_as_unknown(self):
        stamp = "2024-01-01T00:00:00Z"
        report = build_archive_report(
            task_id="t" * 32, plan={"sources": ["era5"]},
            expected_times={"era5": [stamp]}, interval_hours={"era5": 1},
            frame_records=[{"field_id": "a" * 24, "source": "era5", "valid_time": stamp,
                            "status": "completed", "result": frame(0)}],
            cached_fields=[], target_grid=GRID,
            satellite_comparisons=[{"source": "era5", "valid_time": stamp,
                                    "comparison": {"status": "unknown_unmatched", "metrics": None,
                                                   "reason_code": "observation_grid_unavailable"}}],
        )
        satellite = report["satellite_comparison"]
        self.assertEqual("not_supplied", satellite["status"])
        self.assertEqual("unknown_unmatched", satellite["frames"][0]["status"])
        self.assertEqual("observation_grid_unavailable", satellite["frames"][0]["reason_code"])


if __name__ == "__main__":
    unittest.main()
