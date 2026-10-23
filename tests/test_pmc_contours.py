import unittest

import numpy as np

from arktika.pmc_contours import trace_isolines


class PmcContoursTests(unittest.TestCase):
    def test_radial_field_produces_deterministic_closed_contour(self):
        rows, cols = np.mgrid[:15, :17]
        field = (rows - 7.1) ** 2 + (cols - 8.2) ** 2

        first = trace_isolines(field, 16.0)
        second = trace_isolines(field, 16.0)

        self.assertEqual("closed", first["status"])
        self.assertEqual(first, second)
        self.assertEqual(1, first["diagnostics"]["line_count"])
        line = first["lines"][0]
        self.assertTrue(line["closed"])
        self.assertEqual(line["points"][0], line["points"][-1])
        self.assertEqual("row_column_sample_grid", first["coordinate_order"])

    def test_contour_crossing_raster_edge_fails_closed_as_open(self):
        rows, cols = np.mgrid[:7, :8]
        result = trace_isolines(cols.astype(float), 3.25)

        self.assertEqual("open_contour", result["status"])
        self.assertGreater(result["diagnostics"]["open_line_count"], 0)
        self.assertTrue(all(not line["closed"] for line in result["lines"]))

    def test_symmetric_saddle_tie_is_reported_as_ambiguous(self):
        field = np.array([[1.0, -1.0], [-1.0, 1.0]])

        result = trace_isolines(field, 0.0)

        self.assertEqual("ambiguous_saddle", result["status"])
        self.assertEqual(1, result["diagnostics"]["ambiguous_saddle_cells"])
        self.assertEqual([], result["lines"])

    def test_case10_asymptotic_decider_selects_topology_for_both_signs(self):
        negative_determinant = trace_isolines(np.array([[-1.0, 3.0],
                                                         [3.0, -1.0]]), 0.0)
        positive_determinant = trace_isolines(np.array([[-3.0, 1.0],
                                                         [1.0, -3.0]]), 0.0)

        def endpoint_pairs(result):
            return {
                tuple(sorted((tuple(round(value, 3) for value in line["points"][0]),
                              tuple(round(value, 3) for value in line["points"][-1]))))
                for line in result["lines"]
            }

        self.assertEqual("open_contour", negative_determinant["status"])
        self.assertEqual("open_contour", positive_determinant["status"])
        self.assertEqual({((0.0, 0.25), (0.25, 0.0)),
                          ((0.75, 1.0), (1.0, 0.75))},
                         endpoint_pairs(negative_determinant))
        self.assertEqual({((0.0, 0.75), (0.25, 1.0)),
                          ((0.75, 0.0), (1.0, 0.25))},
                         endpoint_pairs(positive_determinant))

    def test_exact_level_vertex_is_not_perturbed_silently(self):
        field = np.array([[0.0, -1.0, -2.0],
                          [1.0, 1.0, 1.0],
                          [2.0, 2.0, 2.0]])

        result = trace_isolines(field, 0.0)

        self.assertEqual("ambiguous_vertex", result["status"])
        self.assertGreater(result["diagnostics"]["exact_level_cells"], 0)

    def test_missing_cells_do_not_create_closed_contours(self):
        rows, cols = np.mgrid[:9, :9]
        field = (rows - 4.1) ** 2 + (cols - 4.0) ** 2
        valid = np.ones(field.shape, dtype=bool)
        valid[4, 2] = False

        result = trace_isolines(field, 6.0, valid)

        self.assertIn(result["status"], {"open_contour", "incomplete_data"})
        self.assertTrue(result["diagnostics"]["has_incomplete_data"])
        self.assertGreater(result["diagnostics"]["incomplete_cells"], 0)

    def test_invalid_mask_is_rejected_without_geometry(self):
        result = trace_isolines(np.ones((3, 3)), 0.5,
                                np.array([[0, 1, 0], [1, 2, 1], [0, 1, 0]]))

        self.assertEqual("invalid_input", result["status"])
        self.assertEqual([], result["lines"])


if __name__ == "__main__":
    unittest.main()
