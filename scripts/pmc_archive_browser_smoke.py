#!/usr/bin/env python3
"""Native-HTTP UI smoke for experimental PMC archive planning and diagnostics."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

import numpy as np
import rasterio
from rasterio.transform import from_origin

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from playwright.sync_api import sync_playwright

from arktika.pmc_archive_run import build_archive_report
from arktika.pmc_detection import ALGORITHM_VERSION, METHOD_REFERENCE, PARAMETERS
from arktika.workstation import Workstation
from server import LocalServer


def install_synthetic_vorticity(app):
    """Use a controlled projected field with the real single and archive detectors."""
    field_id = "d" * 24
    field_dir = app.field_cache / field_id
    field_dir.mkdir()
    cached = b"SYNTHETIC VORTICITY TEST INPUT"
    stamp = "2024-01-01T03:00:00Z"
    (field_dir / "field.nc").write_bytes(cached)
    (field_dir / "metadata.json").write_text(json.dumps({
        "id": field_id, "source": "era5", "time": stamp, "variable": "wind",
        "level": 850, "time_kind": "instantaneous_analysis",
        "sha256": hashlib.sha256(cached).hexdigest(),
    }), encoding="utf-8")

    def projected_field(identity, *, target_grid):
        assert identity == field_id
        values = np.full((target_grid["height"], target_grid["width"]), 2e-4)
        return {"values": values, "grid": target_grid, "passport": {
            "source": "era5", "valid_time": stamp, "level_hPa": 850,
            "variable": "relative_vorticity", "units": "s-1", "grid": target_grid,
            "projection_scale": {"maximum_relative_error": 0.0},
            "input_coverage": {"valid_pixels": values.size},
        }}

    app.experimental_vorticity_850 = projected_field
    return field_id


def synthetic_archive_report(task_id, grid, observation):
    """Use the actual archive report builder with synthetic detector frames."""
    start = "2024-01-01T00:00:00Z"
    end = "2024-01-01T03:00:00Z"
    sources = ["era5", "carra2", "merra2"]
    equation = "(dv/dlambda - cos(phi)*du/dphi + u*sin(phi))/(a*cos(phi))"
    comparison = {
        "status": "comparison_only",
        "observation": {**observation, "grid": grid},
        "coverage": {"observed_pixels": 3, "unknown_pixels": 1},
        "metrics": {"true_positive_pixels": 1, "false_positive_pixels": 0,
                    "false_negative_pixels": 0, "true_negative_pixels": 2,
                    "intersection_over_union": 1.0, "probability_of_detection": 1.0,
                    "false_alarm_ratio": 0.0},
    }
    rows = []
    for field_id, source, stamp, candidate_id in (
        ("a" * 24, "era5", start, "e1"),
        ("b" * 24, "merra2", start, "m1"),
        ("c" * 24, "merra2", end, None),
    ):
        candidates = []
        if candidate_id:
            candidates.append({"candidate_id": candidate_id, "classification": "candidate",
                               "confirmation_status": "not_confirmed", "pixel_count": 1,
                               "peak_pixel_rc": [0, 1], "centroid_pixel_rc": [0.0, 1.0],
                               "peak_850hpa_relative_vorticity_s-1": 1.5e-4})
        satellite = comparison if stamp == start else {
            "status": "unknown_no_eligible_observation",
            "reason_code": "no_eligible_observation", "metrics": None,
        }
        rows.append({"field_id": field_id, "source": source, "valid_time": stamp,
                     "status": "completed", "result": {
                         "source": source, "valid_time": stamp, "grid": grid,
                         "algorithm_version": ALGORITHM_VERSION, "method_reference": METHOD_REFERENCE,
                         "candidate_count": len(candidates), "candidates": candidates,
                         "parameters": dict(PARAMETERS),
                         "field_passport": {"method": {"equation": equation}},
                         "satellite_comparison": satellite,
                     }})
    return build_archive_report(
        task_id=task_id,
        plan={"sources": sources, "start": start, "end": end, "area": [80, -20, 60, 70]},
        expected_times={"era5": [f"2024-01-01T{hour:02d}:00:00Z" for hour in range(4)],
                        "carra2": [start, end], "merra2": [start, end]},
        interval_hours={"era5": 1, "carra2": 3, "merra2": 3},
        frame_records=rows,
        cached_fields=[{"source": row["source"], "time": row["valid_time"],
                        "variable": "wind", "level": 850} for row in rows],
        target_grid=grid,
        satellite_comparisons=[{"source": row["source"], "valid_time": row["valid_time"],
                               "grid": grid, "comparison": row["result"]["satellite_comparison"]}
                              for row in rows],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="browser-results/pmc-archive")
    parser.add_argument("--executable")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": "native-http",
        "fixture": "SYNTHETIC SOFTWARE TEST, NOT OBSERVATIONS",
        "checks": [],
        "browser_errors": [],
        "network": "local test server only; no provider requests or weather data",
    }
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        app = Workstation(root / "state", root / "downloads")
        # The workstation can load ~/.cdsapirc on startup. Keep the smoke local.
        app.era5_credentials({"clear": True})
        server = LocalServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as pw:
                launch = {"headless": True}
                if args.executable:
                    launch["executable_path"] = args.executable
                browser = pw.chromium.launch(**launch)
                page = browser.new_page(viewport={"width": 1440, "height": 960})
                page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
                requests = []
                diagnostic_requests = []

                def capture_request(request):
                    requests.append((request.method, request.url))
                    if request.method == "POST" and request.url.endswith(("/api/pmc/run", "/api/pmc/archive/run")):
                        diagnostic_requests.append(request.post_data_json)

                page.on("request", capture_request)

                def wait(expression, timeout=30):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        try:
                            if page.evaluate(expression):
                                return
                        except Exception:
                            pass
                        page.wait_for_timeout(100)
                    raise AssertionError("Condition not reached: " + expression)

                def check(name, expression):
                    wait(expression)
                    report["checks"].append({"name": name, "passed": True})

                try:
                    page.goto(base + "/#" + server.key)
                    check("Интерфейс запущен", '()=>typeof S!=="undefined"&&S.registry?.products')
                    page.locator("#productRail").click()
                    page.locator('[data-task="pmc_archive"]').click()
                    check(
                        "Форма показывает зарегистрированные источники и ограничения",
                        '()=>document.querySelector("#pmcArchiveDialog").open&&document.querySelectorAll("[name=pmcArchiveSource]").length===3&&document.querySelector("#pmcArchiveDialog").textContent.includes("Экспериментальная диагностика")',
                    )
                    check(
                        "CARRA2 доступен для архивного запуска",
                        '()=>{const e=document.querySelector("[name=pmcArchiveSource][value=carra2]");return !e.disabled&&e.checked}',
                    )
                    check(
                        "Панель импорта показывает обе маски и sidecar",
                        '()=>["pmcObsLabels","pmcObsCoverage","pmcObsMetadata","pmcObservation"].every(id=>document.getElementById(id))',
                    )
                    page.locator("#pmcArchiveDialog").evaluate("el=>el.scrollTop=el.scrollHeight")
                    page.locator("#pmcRunPanel details summary").click()
                    page.locator("#pmcObsPreview").click()
                    check(
                        "Пустой импорт показывает понятную ошибку без запроса",
                        '()=>document.querySelector("#pmcObsStatus").textContent.includes("Выберите labels.tif")',
                    )
                    assert not any(url.endswith("/api/pmc/observations/preview") for _, url in requests)
                    report["checks"].append({"name": "Пустой предпросмотр не отправляет запрос", "passed": True})

                    transform = from_origin(200000, -1000000, 25000, 25000)
                    grid = {"crs":"EPSG:3413", "width":2, "height":2,
                            "transform":list(transform[:6]),
                            "resolution_x_m":25000.0, "resolution_y_m":25000.0}
                    package = root / "package"
                    package.mkdir()
                    for name, values in (("labels", [[0,1],[0,0]]),
                                         ("coverage", [[1,1],[0,1]])):
                        with rasterio.open(package / f"{name}.tif", "w", driver="GTiff",
                                width=2, height=2, count=1, dtype="uint8",
                                crs=grid["crs"], transform=transform) as ds:
                            ds.write(np.asarray(values, dtype=np.uint8), 1)
                    metadata = {"schema_version":"arktika.pmc_satellite_observation.v1",
                        "observation_ref":"smoke-scene-20240101", "source":"smoke-test",
                        "product":"reviewed-labels", "product_version":"1",
                        "valid_time":"2024-01-01T00:00:00Z",
                        "label_convention":"0=no_pmc,1=pmc_candidate",
                        "qc_status":"not_assessed", "grid":grid, "nodata":None}
                    (package / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
                    page.locator("#pmcObsLabels").set_input_files(str(package / "labels.tif"))
                    page.locator("#pmcObsCoverage").set_input_files(str(package / "coverage.tif"))
                    page.locator("#pmcObsMetadata").set_input_files(str(package / "metadata.json"))
                    page.locator("#pmcObsPreview").click()
                    check("Предпросмотр показывает сетку и покрытие без сохранения",
                        '()=>document.querySelector("#pmcObsStatus").textContent.includes("покрытие 75%")&&document.querySelector("#pmcObservation").options.length===1&&document.querySelector("#pmcObsImport").disabled===false')
                    page.locator("#pmcObsImport").click()
                    check("Подтверждение сохраняет пакет и выбирает маску",
                        '()=>document.querySelector("#pmcObsStatus").textContent.includes("imported_unverified")&&document.querySelector("#pmcObservation").options.length===2')
                    check(
                        "Требования спутниковой разметки объяснены",
                        '()=>document.querySelector("#pmcArchiveDialog").textContent.includes("импортируйте спутниковую разметку и маску покрытия")',
                    )

                    page.locator("#pmcArchivePlan").click()
                    check(
                        "Пустой интервал остановлен до запроса",
                        '()=>document.querySelector("#pmcArchiveFeedback").textContent.includes("Укажите начало и конец")',
                    )

                    for selector, value in (
                        ("#pmcArchiveStart", "2024-01-01T00:00:00"),
                        ("#pmcArchiveEnd", "2024-01-01T06:00:00"),
                        ("#pmcNorth", "80"),
                        ("#pmcWest", "-20"),
                        ("#pmcSouth", "60"),
                        ("#pmcEast", "70"),
                    ):
                        page.locator(selector).fill(value)
                    page.locator("#pmcArchivePlan").click()
                    check(
                        "Время без явной зоны UTC отклонено",
                        '()=>document.querySelector("#pmcArchiveFeedback").textContent.includes("формате YYYY-MM-DDTHH:MM:SSZ")',
                    )
                    assert not any(url.endswith("/api/pmc/archive/plan") for _, url in requests)
                    report["checks"].append({"name": "Невалидный UTC не отправлен серверу", "passed": True})

                    page.locator("#pmcArchiveStart").fill("1979-01-01T00:00:00Z")
                    page.locator("#pmcArchiveEnd").fill("1979-01-01T03:00:00Z")
                    for source in ("era5", "carra2", "merra2"):
                        page.locator(f'[name="pmcArchiveSource"][value="{source}"]').uncheck()
                    page.locator('[name="pmcArchiveSource"][value="merra2"]').check()
                    page.locator("#pmcArchivePlan").click()
                    check(
                        "Серверная ошибка диапазона архива показана пользователю",
                        '()=>document.querySelector("#pmcArchiveFeedback").textContent.includes("MERRA-2")',
                    )

                    page.locator("#pmcArchiveStart").fill("2024-01-01T00:00:00Z")
                    page.locator("#pmcArchiveEnd").fill("2024-01-01T06:00:00Z")
                    for source in ("era5", "carra2", "merra2"):
                        page.locator(f'[name="pmcArchiveSource"][value="{source}"]').uncheck()
                    page.locator('[name="pmcArchiveSource"][value="era5"]').check()
                    page.locator("#pmcArchivePlan").click()
                    check(
                        "Успешный план остается экспериментальным и candidate-only",
                        '()=>{const t=document.querySelector("#pmcArchiveFeedback").textContent;return t.includes("Экспериментальный план")&&t.includes("Статус продукции: только кандидаты")&&t.includes("Прогноз не рассчитывается")&&t.includes("Шагов плана: 7")}',
                    )
                    check(
                        "План сообщает о недостающих этапах спутникового контроля",
                        '()=>document.querySelector("#pmcArchiveFeedback").textContent.includes("Нужны атрибутированная наблюдательная маска")',
                    )
                    assert not any("/api/queue" in url or "/api/pmc/archive/run" in url for _, url in requests)
                    report["checks"].append({"name": "План не запускает загрузки или детектор", "passed": True})

                    page.locator("#pmcArchivePrepare").click()
                    check(
                        "Подготовка без реквизитов приостанавливается на конкретном источнике",
                        '()=>document.querySelector("#pmcArchivePrepareTask").textContent.includes("Настроить доступ к ERA5")',
                    )
                    page.locator("#pmcArchivePrepareTask button").click()
                    check(
                        "Для добавления реквизитов открывается штатная форма CDS",
                        '()=>document.querySelector("#cdsAccessDialog").open',
                    )
                    page.locator("#cdsAccessDialog").locator("[data-close]").click()
                    page.locator("#fieldDialog[open]").wait_for(state="visible")
                    page.evaluate("document.querySelector('#fieldDialog')?.close()")
                    check(
                        "Ожидающая реквизитов подготовка доступна для отмены",
                        '()=>!document.querySelector("#pmcArchiveCancel").hidden',
                    )
                    page.locator("#pmcArchiveCancel").click()
                    check(
                        "Подготовку в ожидании реквизитов можно отменить и возобновить",
                        '()=>document.querySelector("#pmcArchivePrepareTask").textContent.includes("Подготовка отменена")&& !document.querySelector("#pmcArchivePrepareResume").hidden',
                    )
                    assert not any(url.endswith("/api/pmc/archive/run") for _, url in requests)
                    report["checks"].append({"name": "Подготовка и детектор остаются раздельными", "passed": True})

                    field_id = install_synthetic_vorticity(app)
                    page.evaluate("async()=>pmcCachedChoices((await api('/api/reanalysis/state')).fields)")
                    page.locator("#pmcRunWidth").select_option("512")
                    check("Настройки порогов свёрнуты по умолчанию",
                          '()=>!document.querySelector("#pmcThresholdSettings").open')
                    page.locator("#pmcRunButton").click()
                    check("Стандартные пороги находят синтетический кандидат",
                          '()=>document.querySelector("#pmcRunResult strong")?.textContent.endsWith("· 1")')
                    assert diagnostic_requests[-1]["field_id"] == field_id
                    assert "thresholds" not in diagnostic_requests[-1]
                    page.locator("#pmcThresholdSettings summary").click()
                    page.locator("#pmcMinimumThreshold").fill("1.5")
                    page.locator("#pmcRunButton").click()
                    check("Пик ниже порога области отклоняется до API",
                          '()=>document.querySelector("#pmcRunResult").textContent.includes("порог пика — не ниже")')
                    assert len(diagnostic_requests) == 1
                    page.locator("#pmcPeakThreshold").fill("3")
                    page.locator("#pmcRunButton").click()
                    check("Пользовательский порог убирает синтетический кандидат",
                          '()=>document.querySelector("#pmcRunResult strong")?.textContent.endsWith("· 0")')
                    thresholds = {"minimum_vorticity_s-1": 1.5e-4, "peak_vorticity_s-1": 3e-4}
                    assert diagnostic_requests[-1]["thresholds"] == thresholds
                    page.locator("#pmcRunResult .pmc-method summary").click()
                    assert "область 1.5e-4 с⁻¹ · пик 3e-4 с⁻¹" in page.locator("#pmcRunResult .pmc-method").inner_text()

                    page.locator("#pmcArchivePlan").click()
                    page.locator(f'[name="pmcArchiveField"][value="{field_id}"]').wait_for()
                    page.locator("#pmcArchiveRun").click()
                    check("Архив обрабатывает срок с пользовательскими порогами", """() => {
                        const task = document.querySelector('#pmcArchiveTask');
                        return task.dataset.status === 'partial' && task.textContent.includes('обработано 1 из 1');
                    }""")
                    assert diagnostic_requests[-1]["thresholds"] == thresholds
                    page.get_by_role("button", name="Показать диагностику и сравнение").click()
                    check("Архивный отчёт показывает применённые пороги и ноль кандидатов", """() => {
                        const text = document.querySelector('#pmcArchiveDiagnostics')?.textContent || '';
                        return text.includes('область 1.5e-4 с⁻¹ · пик 3e-4 с⁻¹') && text.includes('Кандидаты · 0');
                    }""")

                    page.evaluate("document.querySelector('#fieldDialog')?.close()")
                    page.locator('[name="pmcArchiveSource"][value="carra2"]').check()
                    task_id = "e" * 32
                    observation = app.pmc_observations_state()["observations"][0]
                    archive = synthetic_archive_report(task_id, grid, observation)
                    page.route("**/api/pmc/archive/state", lambda route: route.fulfill(json={
                        "id": task_id, "status": "partial", "message": "Синтетический архив",
                        "completed": len(archive["frames"]), "total": len(archive["frames"]),
                    }))
                    page.evaluate("pmcRefreshArchiveTask()")
                    show_diagnostics = page.get_by_role("button", name="Показать диагностику и сравнение")
                    show_diagnostics.click()
                    check("Недоступный отчёт объясняется, повтор остаётся доступен", """() => {
                        const task = document.querySelector('#pmcArchiveTask');
                        const retry = [...task.querySelectorAll('button')].find(
                            button => button.textContent === 'Показать диагностику и сравнение');
                        return task.textContent.includes('ещё не готов') && retry && !retry.disabled;
                    }""")
                    archive_dir = app.pmc_archive_root / task_id
                    archive_dir.mkdir()
                    (archive_dir / "report.json").write_text(json.dumps(archive), encoding="utf-8")
                    show_diagnostics.click()
                    page.locator("#pmcArchiveDiagnostics").wait_for(state="visible")
                    check("Сохранённая диагностика доступна без текущего плана",
                          '()=>document.querySelector("#pmcArchiveRunner").hidden')
                    check("Диагностика показывает покрытие трёх реанализов отдельно", """() => {
                        const coverage = [...document.querySelectorAll('.pmc-source-coverage li')];
                        return [['ERA5', '1 из 4'], ['CARRA2', '0 из 2'], ['MERRA-2', '2 из 2']]
                            .every(([source, count]) => coverage.some(row =>
                                row.textContent.includes(source) && row.textContent.includes(count)));
                    }""")
                    check("Спутниковая сводка разделяет сопоставленные и неизвестные сроки", """() => {
                        const text = document.querySelector('#pmcArchiveDiagnostics').textContent;
                        return text.includes('сопоставлено 2 из 8') && text.includes('неизвестно 6');
                    }""")
                    frame = page.locator("#pmcArchiveFrame")
                    frame.select_option(index=0)
                    frame_details = page.locator("#pmcArchiveFrameDetails")
                    frame_details.get_by_text("Формула и пороги", exact=True).click()
                    details_text = frame_details.inner_text()
                    assert all(expected in details_text for expected in (
                        ALGORITHM_VERSION, "dv/dlambda", "850 гПа", "60 км", "smoke-test", "reviewed-labels",
                    )), details_text
                    report["checks"].append({"name": "Срок показывает формулу, версию и сравнение", "passed": True})
                    frame.select_option(index=2)
                    check("Нулевой результат реанализа отделён от неизвестного спутникового статуса", """() => {
                        const text = document.querySelector('#pmcArchiveFrameDetails').textContent.toLowerCase();
                        return text.includes('кандидаты · 0') && text.includes('спутник: неизвестно') &&
                            !text.includes('reviewed-labels');
                    }""")
                    check(
                        "Отчёт показывает первые появления, точные сравнения и оговорку о genesis",
                        '()=>{const t=document.querySelector(".pmc-onset-evidence").textContent;return t.includes("2024-01-01T00:00:00Z")&&t.includes("smoke-test / reviewed-labels")&&t.includes("не является физическим зарождением")}',
                    )
                    with page.expect_download() as downloaded:
                        page.get_by_role("link", name="Скачать текущий отчёт JSON").click()
                    download = downloaded.value
                    assert download.suggested_filename == f"pmc-archive-{task_id}.json"
                    assert json.loads(download.path().read_text(encoding="utf-8")) == archive
                    report["checks"].append({"name": "JSON экспорт сохраняет полный отчёт", "passed": True})

                    frame.select_option(index=0)
                    frame_details.get_by_role("button", name="Показать пики на карте").click()
                    check("Карта показывает модельный пик с источником и сроком", """() => {
                        const layer = document.querySelector('#pmcOverlay');
                        const label = layer?.getAttribute('aria-label') || '';
                        return !document.querySelector('#pmcArchiveDialog').open &&
                            layer?.querySelectorAll('[data-candidate=e1]').length === 1 &&
                            label.includes('ERA5') && label.includes('2024-01-01') && label.includes('UTC');
                    }""")
                    page.locator('[data-task="pmc_archive"]').click()
                    show_diagnostics.click()
                    page.locator("#pmcArchiveDiagnostics").wait_for(state="visible")
                    frame_details.get_by_text("Формула и пороги", exact=True).click()
                    page.set_viewport_size({"width": 1024, "height": 1150})
                    page.locator("#pmcArchiveDiagnostics").screenshot(
                        path=str(output / "pmc-diagnostics-1024.png"),
                    )
                    page.locator(".pmc-onset-evidence summary").click()
                    for width in (1024, 390):
                        page.set_viewport_size({"width": width, "height": 844})
                        check(
                            f"Форма помещается в окно шириной {width}px",
                            '()=>document.documentElement.scrollWidth<=innerWidth+1&&document.querySelector("#pmcArchiveDialog").scrollWidth<=document.querySelector("#pmcArchiveDialog").clientWidth+1',
                        )
                        page.screenshot(path=str(output / f"pmc-{width}.png"))
                    check("Мобильная форма остается открытой", '()=>document.querySelector("#pmcArchiveDialog").open')
                    assert not report["browser_errors"], report["browser_errors"]
                except Exception:
                    page.screenshot(path=str(output / "failure.png"))
                    raise
                finally:
                    browser.close()
        except Exception as exc:
            report["error"] = str(exc).replace(server.key, "[SESSION]")
            raise
        finally:
            (output / "results.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            server.shutdown()
            thread.join(5)
            server.server_close()
            app.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
