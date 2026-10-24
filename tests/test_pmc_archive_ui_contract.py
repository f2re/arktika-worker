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
        end = source.index("function pmcRenderArchiveFields(", start)
        handler = source[start:end]
        self.assertIn("api('/api/pmc/archive/plan',body)", handler)
        self.assertNotIn("/api/queue", handler)
        self.assertNotIn("/api/pmc/archive/run", handler)
        self.assertIn("Никакие данные не загружены", handler)

    def test_missing_fields_can_be_prepared_without_implicitly_running_detector(self):
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        source = (ROOT / "static/app.js").read_text(encoding="utf-8")
        self.assertIn('id="pmcArchivePrepare"', html)
        prepare = source[source.index("async function pmcPrepareArchive()"):source.index("async function pmcRefreshArchivePrepare(")]
        self.assertIn("api('/api/pmc/archive/prepare'", prepare)
        self.assertIn("api('/api/pmc/archive/prepare/resume'", source)
        self.assertIn("api('/api/pmc/archive/prepare/state'", source)
        self.assertNotIn("/api/pmc/archive/run", prepare)
        self.assertIn("pmc-credentials-saved", source)
        self.assertIn("revision!==S.pmcArchivePlanRevision||S.pmcArchivePlan!==plan", prepare)
        state = source[source.index("async function pmcRefreshArchivePrepare()"):source.index("async function pmcStartArchive(")]
        self.assertIn("$('#pmcArchiveRun').disabled=preparing", state)
        self.assertIn("root.dataset.progress!==progress", state)

    def test_source_rows_are_derived_from_registered_source_catalogue(self):
        source = (ROOT / "static/app.js").read_text(encoding="utf-8")
        self.assertIn("state.catalogue?.sources||[]", source)
        self.assertIn("адаптер зарегистрирован", source)
        self.assertIn("проверяются отдельно", source)

    def test_merra_cadence_advances_to_absolute_utc_cycle(self):
        source = (ROOT / "static/app.js").read_text(encoding="utf-8")
        self.assertIn("new Date(time).getUTCHours()%step!==0", source)
        self.assertIn("time+=step*3600000)expected[source].add", source)
        self.assertIn("api('/api/pmc/archive/grid'", source)
        self.assertIn("area:plan.area", source)

    def test_selected_region_is_bound_to_plan_grid_and_detector_request(self):
        source = (ROOT / "static/app.js").read_text(encoding="utf-8")
        planning = source[source.index("async function planPmcArchive()"):source.index("function pmcRenderArchiveFields(")]
        starting = source[source.index("async function pmcStartArchive()"):source.index("async function pmcRefreshArchiveTask(")]
        self.assertIn("plan.area.some((value,index)=>!Number.isFinite(Number(value))||Number(value)!==body.area[index])", planning)
        self.assertIn("area:plan.area", starting)
        self.assertIn("if(revision!==S.pmcArchivePlanRevision||S.pmcArchivePlan!==plan)throw Error", starting)
        self.assertIn("api('/api/pmc/archive/run',{plan,field_ids,target_grid})", starting)
        # The selected region is part of the plan passed to run; the backend
        # uses that same plan area as the detector's geographic domain mask.
        service = (ROOT / "arktika/reanalysis_service.py").read_text(encoding="utf-8")
        self.assertIn("region_bounds=job['plan']['area']", service)

    def test_replanning_invalidates_old_controls_and_ignores_stale_responses(self):
        source = (ROOT / "static/app.js").read_text(encoding="utf-8")
        invalidation = source[source.index("function pmcInvalidateArchivePlan("):source.index("async function planPmcArchive(")]
        planning = source[source.index("async function planPmcArchive()"):source.index("function pmcRenderArchiveFields(")]
        events = source[source.index("$('#pmcArchiveDialog').addEventListener('change'"):source.index("\n}", source.index("$('#pmcArchiveDialog').addEventListener('change'"))]
        self.assertIn("S.pmcArchivePlan=null", invalidation)
        self.assertIn("runner.hidden=true", invalidation)
        self.assertIn("$('#pmcArchiveFields').replaceChildren()", invalidation)
        self.assertIn("$('#pmcArchiveRun').disabled=true", invalidation)
        self.assertIn("if(revision!==S.pmcArchivePlanRevision)return", planning)
        self.assertIn("if(revision===S.pmcArchivePlanRevision)", planning)
        self.assertIn("pmcInvalidateArchivePlan('Параметры изменились. Составьте план заново.')", events)
        self.assertIn("$('#pmcArchiveRunner').hidden=true", planning)
        self.assertIn("$('#pmcArchiveFields').replaceChildren()", planning)

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

    def test_carra_is_supported_for_single_field_and_archive_runs(self):
        source = (ROOT / "static/app.js").read_text(encoding="utf-8")
        self.assertIn("['era5','carra2','merra2'].includes(f.source)", source)
        self.assertIn("реальные метаданные и метеорологическая точность отдельно не валидированы", source)
        self.assertIn("input.checked=true;const text=document.createElement('span');text.textContent=`${PMC_SOURCE_LABELS[source.id]}", source)
        self.assertIn("['era5','carra2','merra2'].includes(f.source)", source[source.index("async function pmcStartArchive()"):source.index("async function pmcRefreshArchiveTask(")])
        self.assertNotIn("input.disabled=source.id==='carra2'", source)
        self.assertNotIn("['era5','merra2'].includes(field.source)", source)


if __name__ == "__main__":
    unittest.main()
