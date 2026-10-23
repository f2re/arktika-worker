import unittest

from arktika.pmc_tracking import link_candidates


GRID = {
    "crs": "EPSG:3413", "width": 100, "height": 100,
    "resolution_x_m": 10_000, "resolution_y_m": 10_000,
    # Includes rotation/shear to exercise the full affine mapping.
    "transform": [10_000, 1_000, -500_000, 2_000, -10_000, 500_000],
}


def frame(hour, candidates, *, source="ERA5:test", grid=None):
    stamp = f"2025-01-02T{hour:02d}:00:00Z"
    return {
        "source": source,
        "valid_time": stamp,
        "grid": dict(GRID if grid is None else grid),
        "candidates": [dict(c) for c in candidates],
    }


def candidate(identifier, row, col):
    return {"candidate_id": identifier, "centroid_pixel_rc": [row, col],
            "classification": "candidate", "confirmation_status": "not_confirmed"}


class PmcTrackingTests(unittest.TestCase):
    def test_links_moving_vortex_with_affine_metric_distance_and_censored_start(self):
        outputs = link_candidates([
            frame(0, [candidate("a", 10, 10)]),
            frame(1, [candidate("b", 10, 11)]),
            frame(2, [candidate("c", 11, 12)]),
        ])
        self.assertEqual(1, outputs["track_count"])
        track = outputs["tracks"][0]
        self.assertEqual("pmc-track-0001", track["track_id"])
        self.assertTrue(track["track_start_observed"])
        self.assertTrue(track["left_censored"])
        self.assertEqual(["a", "b", "c"], [p["candidate_id"] for p in track["points"]])
        # One pixel column step uses the rotated affine vector (10km, 2km).
        self.assertAlmostEqual((10_000**2 + 2_000**2) ** 0.5,
                               track["points"][1]["link_distance_m"])
        self.assertEqual("candidate_only", track["classification"])
        self.assertEqual("not_confirmed", track["confirmation_status"])
        self.assertNotIn("genesis_location", track)
        self.assertEqual("1.0", outputs["passport"]["schema_version"])
        self.assertEqual({"start": "2025-01-02T00:00:00Z", "end": "2025-01-02T02:00:00Z"},
                         outputs["passport"]["valid_period"])
        self.assertEqual("not_assessed", outputs["passport"]["uncertainty"]["status"])

    def test_gap_over_one_configured_step_rejected(self):
        with self.assertRaisesRegex(ValueError, "gap"):
            link_candidates([frame(0, [candidate("a", 1, 1)]),
                             frame(2, [candidate("b", 1, 2)])])

    def test_candidate_centroids_must_be_inside_grid(self):
        for row, col in ((-0.1, 1), (100, 1), (1, -0.1), (1, 100)):
            with self.subTest(row=row, col=col), self.assertRaisesRegex(ValueError, "grid bounds"):
                link_candidates([frame(0, [candidate("outside", row, col)])])

    def test_numeric_limits_reject_bool_string_and_nonfinite_values(self):
        frames = [frame(0, [])]
        for option in ("max_step_seconds", "max_distance_m"):
            for invalid in (True, "3600", float("nan"), float("inf"), 0, -1):
                with self.subTest(option=option, invalid=invalid), self.assertRaisesRegex(ValueError, "finite positive number"):
                    link_candidates(frames, **{option: invalid})

    def test_non_dict_frame_rejected_before_metadata_access(self):
        with self.assertRaisesRegex(ValueError, "frame must be"):
            link_candidates([None])

    def test_grid_dimensions_must_be_positive_integers(self):
        for value in (True, 1.5, 0, -1, float("inf")):
            bad_grid = dict(GRID, width=value)
            with self.subTest(width=value), self.assertRaises(ValueError):
                link_candidates([frame(0, [], grid=bad_grid)])

    def test_duplicate_or_non_monotonic_times_rejected(self):
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            link_candidates([frame(1, []), frame(1, [])])
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            link_candidates([frame(2, []), frame(1, [])])
        with self.assertRaisesRegex(ValueError, "UTC"):
            bad = frame(1, [])
            bad["valid_time"] = "2025-01-02T02:00:00+02:00"
            link_candidates([bad])

    def test_source_and_full_grid_must_match(self):
        with self.assertRaisesRegex(ValueError, "same source and grid"):
            link_candidates([frame(0, []), frame(1, [], source="CARRA:test")])
        shifted = dict(GRID, transform=[10_000, 1_000, -490_000, 2_000, -10_000, 500_000])
        with self.assertRaisesRegex(ValueError, "same source and grid"):
            link_candidates([frame(0, []), frame(1, [], grid=shifted)])

    def test_no_candidates_returns_empty_not_fake_tracks(self):
        outputs = link_candidates([frame(0, []), frame(1, [])])
        self.assertEqual(0, outputs["track_count"])
        self.assertEqual([], outputs["tracks"])
        self.assertEqual("candidate_track_catalog", outputs["passport"]["quantity_kind"])

    def test_empty_input_is_not_promoted_to_a_product_without_time_or_provenance(self):
        outputs = link_candidates([])
        self.assertEqual(0, outputs["track_count"])
        self.assertNotIn("passport", outputs)

    def test_unmatched_after_empty_frame_starts_new_left_censored_track(self):
        outputs = link_candidates([frame(0, [candidate("a", 1, 1)]),
                                   frame(1, []),
                                   frame(2, [candidate("b", 1, 2)])])
        self.assertEqual(2, outputs["track_count"])
        self.assertFalse(outputs["tracks"][1]["left_censored"])
        self.assertEqual(1, len(outputs["tracks"][1]["points"]))

    def test_ambiguous_split_merge_is_reported_as_unresolved(self):
        outputs = link_candidates([frame(0, [candidate("a", 5, 5)]),
                                   frame(1, [candidate("b", 5, 4), candidate("c", 5, 6)])])
        self.assertEqual(3, outputs["track_count"])
        self.assertEqual("unresolved_split_merge_association",
                         outputs["unresolved_associations"][0]["status"])
        self.assertEqual([["a"], ["b"], ["c"]], [
            [point["candidate_id"] for point in track["points"]]
            for track in outputs["tracks"]])
        association = outputs["unresolved_associations"][0]
        self.assertEqual("no_tracks_continued_or_created_from_ambiguous_links", association["action"])
        self.assertEqual({"b", "c"}, {item["current_candidate_id"]
                                       for item in association["possible_links"]})
        self.assertTrue(any("пар 1:1" in item for item in outputs["limitations"]))

    def test_ambiguous_merge_does_not_pick_nearest_previous_track(self):
        outputs = link_candidates([frame(0, [candidate("a", 5, 4), candidate("b", 5, 6)]),
                                   frame(1, [candidate("c", 5, 5)])])
        self.assertEqual(3, outputs["track_count"])
        self.assertEqual([["a"], ["b"], ["c"]], [
            [point["candidate_id"] for point in track["points"]]
            for track in outputs["tracks"]])
        self.assertEqual({"a", "b"}, {item["previous_candidate_id"]
                                       for item in outputs["unresolved_associations"][0]["possible_links"]})

    def test_independent_one_to_one_link_continues_beside_ambiguous_component(self):
        outputs = link_candidates([
            frame(0, [candidate("a", 5, 5), candidate("x", 30, 30)]),
            frame(1, [candidate("b", 5, 4), candidate("c", 5, 6), candidate("y", 30, 31)]),
        ])
        tracks = {tuple(point["candidate_id"] for point in track["points"])
                  for track in outputs["tracks"]}
        self.assertEqual({("a",), ("b",), ("c",), ("x", "y")}, tracks)
        self.assertEqual(1, len(outputs["unresolved_associations"]))

    def test_disconnected_merge_and_split_are_reported_as_separate_components(self):
        outputs = link_candidates([
            frame(0, [candidate("a", 5, 4), candidate("b", 5, 6),
                      candidate("x", 30, 30)]),
            frame(1, [candidate("c", 5, 5), candidate("y", 30, 29),
                      candidate("z", 30, 31)]),
        ])
        associations = outputs["unresolved_associations"]
        self.assertEqual(2, len(associations))
        self.assertEqual({("a", "b")}, {
            tuple(sorted(item["previous_candidate_id"] for item in row["possible_links"]))
            for row in associations if row["current_candidates"][0]["candidate_id"] == "c"})
        self.assertEqual({("y", "z")}, {
            tuple(sorted(item["current_candidate_id"] for item in row["possible_links"]))
            for row in associations if row["previous_candidates"][0]["candidate_id"] == "x"})


if __name__ == "__main__":
    unittest.main()
