#!/usr/bin/env python3
"""Native-HTTP UI smoke for experimental PMC archive planning only."""
import argparse
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

from arktika.workstation import Workstation
from server import LocalServer


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
                page.on("request", lambda request: requests.append((request.method, request.url)))

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
                        '()=>document.querySelector("#pmcArchiveDialog").open&&document.querySelectorAll("[name=pmcArchiveSource]").length===3&&document.querySelector("#pmcArchiveDialog").textContent.includes("не прогноз")',
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
                        '()=>document.querySelector("#pmcArchiveDialog").textContent.includes("размеченная маска")',
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

                    page.evaluate("document.querySelector('#fieldDialog')?.close()")
                    page.evaluate("""() => {
                      api = async () => ({tracks_by_source:[{source:'era5',candidate_emergence_evidence:[{
                        source:'era5',first_detected_time:'2024-01-01T01:00:00Z',candidate_id:'e1',
                        prior_frame:{status:'previous_processed_frame_had_no_model_candidates'},
                        cross_source_evidence_at_first_detection:{status:'compared_exact_utc',pairs:[{other_source:'merra2',status:'matched_candidate',matches:[{candidate_id:'m1'}]}]},
                        satellite_evidence_at_first_detection:{status:'comparison_only',observation:{source:'manual',product_id:'labels-v1',observation_id:'o'.repeat(24)},coverage:{observed_pixels:20}}
                      }]}]});
                      pmcShowArchiveEvidence('synthetic-task',document.querySelector('#pmcArchiveTask'));
                    }""")
                    page.locator("#pmcArchiveTask button").click()
                    check(
                        "Отчёт показывает первые появления, точные сравнения и оговорку о genesis",
                        '()=>{const t=document.querySelector("#pmcArchiveTask").textContent;return t.includes("2024-01-01T01:00:00Z")&&t.includes("merra2: совпадение m1")&&t.includes("manual / labels-v1")&&t.includes("не является физическим зарождением")}',
                    )

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
