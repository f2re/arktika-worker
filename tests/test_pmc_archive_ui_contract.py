"""Guard the user-facing contract for experimental PMC archive planning."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PmcArchiveUiContractTests(unittest.TestCase):
    def test_form_collects_explicit_sources_utc_range_and_geographic_area(self):
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        for element_id in (
            "pmcArchiveDialog", "pmcArchiveSources", "pmcArchiveStart",
            "pmcArchiveEnd", "pmcNorth", "pmcWest", "pmcSouth", "pmcEast",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', html)
        self.assertIn("Экспериментальный план", html)
        self.assertIn("не прогноз", html)
        self.assertIn("совпадающей сеткой", html)

    def test_plan_is_a_plan_request_not_a_download_or_detector_run(self):
        source = (ROOT / "static/app.js").read_text(encoding="utf-8")
        start = source.index("async function planPmcArchive()")
        end = source.index("function registerEvents()", start)
        handler = source[start:end]
        self.assertIn("api('/api/pmc/archive/plan',body)", handler)
        self.assertNotIn("/api/queue", handler)
        self.assertNotIn("/api/pmc/archive/run", handler)
        self.assertIn("Никакие данные не загружены", handler)

    def test_source_rows_are_derived_from_registered_source_catalogue(self):
        source = (ROOT / "static/app.js").read_text(encoding="utf-8")
        self.assertIn("state.catalogue?.sources||[]", source)
        self.assertIn("адаптер зарегистрирован", source)
        self.assertIn("проверяются отдельно", source)

    def test_plan_displays_server_reported_source_and_maturity_status(self):
        source = (ROOT / "static/app.js").read_text(encoding="utf-8")
        start = source.index("async function planPmcArchive()")
        end = source.index("function registerEvents()", start)
        handler = source[start:end]
        for contract_field in (
            "result.maturity", "result.output_status", "result.forecast",
            "result.sources", "credential_status", "support_status",
            "result.satellite", "result.missing_inputs", "result.limitations",
        ):
            with self.subTest(contract_field=contract_field):
                self.assertIn(contract_field, handler)


if __name__ == "__main__":
    unittest.main()
