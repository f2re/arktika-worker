"""Archive planning validates inputs without submitting requests or inventing results."""
from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from arktika.pmc_archive import build_plan
from arktika.workstation import Workstation


def request(**changes):
    value = {"sources": ["era5", "carra2", "merra2"],
             "start": "2024-01-01T00:00:00Z", "end": "2024-01-01T06:00:00Z",
             "area": [80, -20, 60, 70]}
    value.update(changes)
    return value


class PmcArchivePlanTests(unittest.TestCase):
    def test_plan_is_candidate_only_and_counts_each_source_native_cadence(self):
        plan = build_plan(request(), credentials={"era5": True}, cached_fields=[])
        self.assertEqual(plan["maturity"], "experimental")
        self.assertEqual(plan["output_status"], "candidate_only")
        self.assertFalse(plan["forecast"])
        sources = {row["id"]: row for row in plan["sources"]}
        self.assertEqual(sources["era5"]["step_count"], 7)
        self.assertEqual(sources["carra2"]["step_count"], 3)
        self.assertEqual(sources["merra2"]["step_count"], 3)
        self.assertEqual(sources["era5"]["credential_status"], "ready")
        self.assertEqual(sources["merra2"]["credential_status"], "required")
        self.assertEqual(plan["satellite"]["status"], "manual_mask_required")
        self.assertIn("не запускает расчёт", plan["missing_inputs"][0])

    def test_matching_cached_wind_fields_are_reported_but_not_treated_as_derived_results(self):
        fields = [{"source": "era5", "variable": "wind", "level": 850,
                   "time": "2024-01-01T00:00:00Z"}]
        plan = build_plan(request(sources=["era5"]), credentials={}, cached_fields=fields)
        self.assertEqual(plan["sources"][0]["cached_step_count"], 1)
        self.assertEqual(plan["sources"][0]["missing_step_count"], 6)
        self.assertEqual(plan["output_status"], "candidate_only")

    def test_rejects_naive_times_bad_sources_future_and_invalid_area(self):
        for changes in (
            {"start": "2024-01-01T00:00:00"},
            {"sources": ["unknown"]},
            {"area": [39, -20, 30, 70]},
            {"start": "2024-01-01T06:00:00Z", "end": "2024-01-01T00:00:00Z"},
            {"sources": ["carra2"], "start": "1980-01-01T00:00:00Z"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_plan(request(**changes), credentials={}, cached_fields=[])

    def test_workstation_api_returns_only_plan_without_creating_a_job_or_product(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Workstation(root / "state", download_dir=root / "downloads")
            try:
                result = app.pmc_archive_plan(request(sources=["era5"]))
                self.assertEqual(result["output_status"], "candidate_only")
                self.assertFalse(app.busy)
                self.assertEqual(list(app.fields_state()["fields"]), [])
                self.assertEqual(app.products(), [])
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
