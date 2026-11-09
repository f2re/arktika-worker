#!/usr/bin/env python3
"""Real local HTTP/browser workflow on explicitly synthetic numerical wind.

No detector monkeypatches, provider downloads, or meteorological validation.
Imports NetCDF, runs the real derivative/detector, annotates, exports PNG and
restores a persistent bookmark after a browser reload.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading

import numpy as np
import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "scripts")]

from arktika.geo import grid
from arktika.workstation import Workstation
from server import LocalServer


def install_numerical_vortex(app, root):
    """Gaussian cyclonic wind with known positive centre, not observed weather."""
    latitude = np.arange(60., 84.01, .25)
    longitude = np.arange(-25., 70.01, .25)
    lat, lon = np.meshgrid(latitude, longitude, indexing="ij")
    earth_radius = 6371000.
    x = earth_radius * np.cos(np.deg2rad(72.)) * np.deg2rad(lon - 25.)
    y = earth_radius * np.deg2rad(lat - 72.)
    envelope = np.exp(-(x*x + y*y)/(2*200000.**2))
    u, v = -2e-4*y*envelope, 2e-4*x*envelope
    coords = {"time": np.array(["2024-01-01T00:00:00"], dtype="datetime64[ns]"),
              "latitude": ("latitude", latitude, {"units": "degrees_north"}),
              "longitude": ("longitude", longitude, {"units": "degrees_east"})}
    pressure = dict(coords, pressure_level=("pressure_level", [850.], {"units": "hPa"}))
    wind = xr.Dataset({
        "u": (("time", "pressure_level", "latitude", "longitude"), u[None, None].astype("float32"),
              {"units": "m s-1", "standard_name": "eastward_wind"}),
        "v": (("time", "pressure_level", "latitude", "longitude"), v[None, None].astype("float32"),
              {"units": "m s-1", "standard_name": "northward_wind"})}, coords=pressure,
        attrs={"title": "SYNTHETIC NUMERICAL VORTEX; NOT WEATHER OBSERVATIONS"})
    mslp = xr.Dataset({"msl": (("time", "latitude", "longitude"),
                    (101500.-2000.*envelope)[None].astype("float32"), {"units": "Pa"})},
                    coords=coords, attrs={"title": "SYNTHETIC PRESSURE FOR SOFTWARE TEST"})
    identities = {}
    for variable, data, level in (("wind", wind, 850), ("mslp", mslp, None)):
        path = root / ("synthetic-" + variable + ".nc")
        data.to_netcdf(path, engine="scipy")
        app.fields_add({"source": "era5", "variable": variable, "level": level,
                        "time": "2024-01-01T00:00:00Z", "area": [84, -25, 60, 70],
                        "path": str(path)}, local=True)
        app.task.join(60)
        state = app.fields_state()
        assert state["job"]["status"] == "done", state["job"]
        identities[variable] = state["job"]["field_id"]
    return identities


def numerical_check(app, identity):
    g = grid("barents", 512)
    target = {"crs": g["crs"], "width": g["width"], "height": g["height"],
              "transform": list(g["transform"][:6]),
              "resolution_x_m": g["transform"].a, "resolution_y_m": -g["transform"].e}
    result = app.experimental_pmc_candidates(identity, target_grid=target)
    assert result["candidate_count"] >= 1, result["candidate_count"]
    assert all(c["confirmation_status"] == "not_confirmed" for c in result["candidates"])
    assert any(c["geometry"]["status"] == "complete" for c in result["candidates"])
    return result["candidate_count"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="browser-results/pmc-workspace")
    parser.add_argument("--executable")
    parser.add_argument("--fixture-only", action="store_true", help="Run real numerical path without a browser")
    args = parser.parse_args()
    output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
    report = {"mode": "numerical-only" if args.fixture_only else "native-http",
              "fixture": "SYNTHETIC NUMERICAL VORTEX; NOT WEATHER OBSERVATIONS",
              "checks": [], "browser_errors": [],
              "network": "localhost only; no CDS/Earthdata/provider requests"}
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        app = Workstation(root / "state", root / "downloads")
        app.era5_credentials({"clear": True})
        server = None
        try:
            identities = install_numerical_vortex(app, root)
            count = numerical_check(app, identities["wind"])
            report["checks"].append({"name": "NetCDF → реальная завихрённость → кандидаты и контуры", "passed": True, "candidate_count": count})
            if args.fixture_only:
                print(json.dumps(report, ensure_ascii=False, indent=2)); return
            from playwright.sync_api import sync_playwright
            server = LocalServer(("127.0.0.1", 0), app)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}"
            with sync_playwright() as pw:
                launch = {"headless": True}
                if args.executable: launch["executable_path"] = args.executable
                browser = pw.chromium.launch(**launch)
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
                page = context.new_page()
                page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
                requests = []
                page.on("request", lambda request: requests.append((request.method, request.url)))
                def check(name, expression):
                    page.wait_for_function(expression, timeout=60000)
                    report["checks"].append({"name": name, "passed": True})
                    print(name, flush=True)
                page.goto(base + "/#" + server.key)
                check("Запуск с новым рабочим местом ПМЦ", "()=>S.map&&FIELDS.fields.length===2&&document.querySelector('#pmcWorkspacePanel')")
                page.locator("#pmcWorkspaceOpen").click()
                check("Пустое состояние и календарь доступны", "()=>document.querySelector('#calendar').children.length>20&&document.querySelector('#pmcCurrentAnalysis').textContent.includes('ещё не открыт')")
                page.locator("#pmcBookmarksOpen").click()
                check("Пустой список закладок объяснён", "()=>document.querySelector('#pmcBookmarkList').textContent.includes('пока нет')")
                page.locator("#pmcBookmarksClose").click()
                page.locator("#pmcPanelAnalysis").click()
                page.locator("#pmcCachedField").select_option(identities["wind"])
                page.locator("#pmcRunCrs").select_option("barents")
                page.locator("#pmcRunWidth").select_option("512")
                page.locator("#pmcRunButton").click()
                check("Численный расчёт автоматически выбирает проекцию и наносит область", "()=>PMCW.result?.candidate_count>0&&S.map.grid.crs==='EPSG:3995'&&document.querySelector('#pmcOverlay [data-candidate-region]')")
                page.locator("#pmcRunResult button").filter(has_text="Перейти к карте").click()
                page.locator("#pmcReviewFields summary").click()
                page.locator("#pmcContextMap").click()
                check("Поля того же срока нанесены штатным рендером", "()=>FIELDS.stack.length===2&&document.querySelectorAll('#reanalysisLayers>g').length===2")
                # Include a real raster in PNG, not only vector contours/arrows.
                page.evaluate("async()=>{FIELDS.stack.find(s=>fieldMeta(s.id).variable==='wind').style='fill_arrows';await saveFieldStack();drawFieldStack();await renderFieldMaps();}")
                page.locator("#pmcCandidateList button").first.click()
                page.locator("#pmcAnnotationStatus").select_option("expert_supported")
                page.locator("#pmcAnnotationForm button[type=submit]").click()
                check("Экспертная оценка требует основания", "()=>document.querySelector('#pmcAnnotationError').textContent.includes('основание')&&PMCW.annotations[0].status==='manual_candidate'")
                page.locator("#pmcAnnotationLabel").fill("СИНТЕТИЧЕСКИЙ СЛУЧАЙ <img src=x onerror=alert(1)>")
                page.locator("#pmcAnnotationNote").fill("Только проверка интерфейса на синтетических полях; метеорологическая точность не оценивалась.")
                page.locator("#pmcAnnotationForm button[type=submit]").click()
                check("Наноска отделена от научного результата и безопасна как текст", "()=>PMCW.annotations[0].status==='expert_supported'&&PMCW.result.candidates[0].confirmation_status==='not_confirmed'&&!document.querySelector('#pmcAnnotations img')")
                page.locator("#pmcWorkspaceTitle").fill("СИНТЕТИЧЕСКИЙ ТЕСТ — ПМЦ и закладка")
                page.locator("#pmcWorkspaceNotes").fill("Пример не является реальным атмосферным случаем.")
                page.locator("#zoomIn").click()
                original_view = page.evaluate("S.view")
                with page.expect_download() as download_info:
                    page.locator("#pmcPngExport").click()
                downloaded = download_info.value
                png_path = output / "01-synthetic-analysis.png"
                downloaded.save_as(png_path)
                assert png_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
                from PIL import Image
                with Image.open(png_path) as image:
                    assert image.width == 1600 and image.height > 1000
                    assert len(image.convert("RGB").getcolors(image.width*image.height)) > 100
                report["checks"].append({"name": "PNG действительно скачан, содержит карту, растр и легенду", "passed": True})
                page.locator("#pmcBookmarkSave").click()
                check("Закладка подтверждена сервером", "()=>PMCW.bookmarkId&&!PMCW.dirty")
                bookmark_id = page.evaluate("PMCW.bookmarkId")
                # Keep the real HTTP save, delay only delivery of its response.
                page.locator("#pmcWorkspaceNotes").fill("Сохранённый снимок до задержанного ответа")
                page.evaluate("""()=>{window.__pmcOriginalApi=api;window.__pmcReleaseSave=null;
                    api=async function(path,body){const response=await window.__pmcOriginalApi(path,body);
                      if(path==='/api/pmc/bookmarks/save')await new Promise(resolve=>window.__pmcReleaseSave=resolve);
                      return response;};}""")
                page.locator("#pmcBookmarkSave").click()
                page.wait_for_function("()=>typeof window.__pmcReleaseSave==='function'")
                page.locator("#pmcWorkspaceNotes").fill("Более поздняя правка ещё не сохранена")
                page.evaluate("()=>{api=window.__pmcOriginalApi;window.__pmcReleaseSave();}")
                check("Поздний ответ сохранения не объявляет новые правки сохранёнными", "()=>PMCW.dirty&&document.querySelector('#pmcWorkspaceStatus').textContent.includes('ещё не сохранены')")
                saved_notes = page.evaluate("async()=> (await api('/api/pmc/bookmark?id='+PMCW.bookmarkId)).bookmark.notes")
                assert saved_notes == "Сохранённый снимок до задержанного ответа"
                page.locator("#pmcBookmarkSave").click()
                check("Новые изменения сохраняются отдельным действием", "()=>!PMCW.dirty&&!document.querySelector('#pmcBookmarkSave').disabled")
                page.screenshot(path=str(output / "02-workspace-desktop.png"))
                before = len([url for method, url in requests if method == "POST" and url.endswith("/api/pmc/run")])
                page.reload()
                check("Повторная загрузка приложения завершена", "()=>S.map&&FIELDS.fields.length===2")
                page.locator("#pmcWorkspaceOpen").click()
                page.locator("#pmcBookmarksOpen").click()
                page.locator("#pmcBookmarkList button").filter(has_text="Открыть анализ").click()
                check("Закладка восстанавливает результат и ручную оценку", "()=>PMCW.restored&&PMCW.annotations.length===1&&PMCW.annotations[0].status==='expert_supported'&&document.querySelector('#pmcOverlay [data-candidate-region]')&&FIELDS.stack.length===2")
                assert page.evaluate("S.view") == original_view
                assert page.evaluate("PMCW.bookmarkId") == bookmark_id
                assert before == len([url for method, url in requests if method == "POST" and url.endswith("/api/pmc/run")])
                report["checks"].append({"name": "Восстановлены масштаб и ID без повторного расчёта", "passed": True})
                retained = page.evaluate("JSON.stringify([PMCW.result,PMCW.annotations,PMCW.bookmarkId,document.querySelector('#pmcWorkspaceNotes').value])")
                map_calls = [0]
                def fail_second_map(route):
                    map_calls[0] += 1
                    if map_calls[0] == 2:
                        route.fulfill(status=500, content_type="application/json", body='{"error":"test restore failure"}')
                    else:
                        route.continue_()
                page.route("**/api/map?*", fail_second_map)
                page.evaluate("""()=>{window.__pmcRestoreDone=false;window.__pmcRestoreError=null;
                    pmcRestoreBookmark(PMCW.bookmarkId).catch(error=>window.__pmcRestoreError=error.message)
                      .finally(()=>window.__pmcRestoreDone=true);}""")
                check("Ошибка после начала восстановления безопасно откатывается", "()=>window.__pmcRestoreDone&&window.__pmcRestoreError==='test restore failure'&&!PMCW.restoring")
                assert page.evaluate("JSON.stringify([PMCW.result,PMCW.annotations,PMCW.bookmarkId,document.querySelector('#pmcWorkspaceNotes').value])") == retained
                page.unroute("**/api/map?*", fail_second_map)
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(output / "03-workspace-mobile.png"))
                check("Узкое окно не выходит за границы страницы", "()=>document.documentElement.scrollWidth<=innerWidth+1")
                page.locator("#closeLeft").click()
                page.locator("#pmcDrawToggle").click()
                check("Наноска включена с доступной картой на узком экране", "()=>PMCW.drawing&&document.querySelector('#leftPanel').hidden")
                page.keyboard.press("Escape")
                check("Esc завершает наноску", "()=>!PMCW.drawing")
                page.set_viewport_size({"width": 1440, "height": 1000})
                # Errors are shown in the UI without claiming successful export.
                page.route("**/field-render/**", lambda route: route.fulfill(status=500, body="test unavailable"))
                page.locator("#pmcPngExport").click()
                check("Сбой слоя отменяет PNG с явным сообщением", "()=>document.querySelector('#pmcWorkspaceStatus').textContent.includes('Не удалось сохранить PNG')")
                page.unroute("**/field-render/**")
                assert not report["browser_errors"], report["browser_errors"]
                assert all(url.startswith((base, "blob:", "data:")) for _, url in requests), "Unexpected external request"
                browser.close()
        finally:
            if server is not None: server.shutdown(); server.server_close()
            app.close()
            (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
