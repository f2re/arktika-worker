#!/usr/bin/env python3
"""Local HTTP/browser acceptance using attributed real reanalysis files.

Requires existing NetCDF inputs and a provenance manifest with SHA256 hashes.
Does not generate or substitute weather values. Imports the real fields, runs
the numerical detector, annotates, exports PNG and restores a persistent bookmark.
Numerical candidates are never presented as independently confirmed polar lows.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "scripts")]

from arktika.geo import grid
from arktika.workstation import Workstation
from server import LocalServer


@contextmanager
def browser_failure_artifacts(get_page, output, report):
    """Capture evidence before Playwright tears down a failed browser session."""
    try:
        yield
    except Exception as error:
        failure = {"error_type": type(error).__name__, "error": str(error),
                   "active_check": report.get("active_check"),
                   "last_passed_check": report["checks"][-1]["name"] if report["checks"] else None}
        page = get_page()
        if page is not None and not page.is_closed():
            try:
                failure["state"] = page.evaluate("""()=>{
                    const text=id=>document.querySelector(id)?.textContent||'',
                      calendar=document.querySelector('#calendar'),
                      state=typeof S==='undefined'?null:S,
                      workspace=typeof PMCW==='undefined'?null:PMCW;
                    return {url:location.origin+location.pathname,day:state?.day,month:state?.month,
                      server_state_ready:!!state?.serverState,server_last_date:state?.serverState?.last_date,
                      map:state?.map?.grid,calendar_children:calendar?.children.length,
                      calendar_first_date:calendar?.querySelector('[data-date]')?.dataset.date,
                      calendar_selected_date:calendar?.querySelector('.selected[data-date]')?.dataset.date,
                      date_value:document.querySelector('#date')?.value,date_caption:text('#dateCaption'),
                      pmc_current_analysis:text('#pmcCurrentAnalysis'),pmc_status:text('#pmcWorkspaceStatus'),
                      run_result:text('#pmcRunResult'),annotation_error:text('#pmcAnnotationError'),
                      status:text('#status'),toast:text('#toast'),
                      pmc:workspace?{restoring:workspace.restoring,restored:workspace.restored,
                        candidate_count:workspace.result?.candidate_count,valid_time:workspace.result?.valid_time,
                        annotation_count:workspace.annotations.length,bookmark_id:workspace.bookmarkId}:null};
                }""")
            except Exception as diagnostic_error:
                failure["state_error"] = str(diagnostic_error)
            try:
                page.screenshot(path=str(output / "failure.png"), timeout=10000)
                failure["screenshot"] = "failure.png"
            except Exception as screenshot_error:
                failure["screenshot_error"] = str(screenshot_error)
        report["failure"] = failure
        (output / "failure.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(failure, ensure_ascii=False), flush=True)
        raise


def install_input_fields(app, args):
    """Validate the attribution/hash manifest, then use the normal importer."""
    provenance = json.loads(Path(args.provenance).read_text(encoding="utf-8"))
    if (provenance.get("schema_version") != "arktika.real_reanalysis_case.v1"
            or provenance.get("data_kind") != "analysis"
            or provenance.get("source") != args.source
            or provenance.get("time") != args.time
            or provenance.get("area") != args.area):
        raise ValueError("Паспорт реального реанализа не соответствует источнику, сроку или области запроса.")
    if not provenance.get("upstream_objects"):
        raise ValueError("В паспорте отсутствуют исходные объекты публичного архива.")
    fields = [("wind", Path(args.input_netcdf), 850)]
    if args.pressure_netcdf:
        fields.append(("mslp", Path(args.pressure_netcdf), None))
    available = {"t500": ("t", 500), "wind10": ("wind10", None),
                 "sst": ("sst", None), "ice": ("ice", None), "lsm": ("lsm", None)}
    for item in args.field:
        key, separator, filename = item.partition("=")
        if not separator or key not in available or not filename:
            raise ValueError("Поле задаётся как t500=FILE, wind10=FILE, sst=FILE, ice=FILE или lsm=FILE.")
        variable, level = available[key]
        if any(field[0] == variable for field in fields):
            raise ValueError("Поле задано повторно: " + variable)
        fields.append((variable, Path(filename), level))
    identities, verified, hashes = {}, [], {}
    for variable, path, level in fields:
        path = path.resolve()
        if path not in hashes:
            with path.open("rb") as stream:
                hashes[path] = hashlib.file_digest(stream, "sha256").hexdigest()
        records = [item for item in provenance.get("fields", [])
                   if item.get("variable") == variable and item.get("level") == level
                   and item.get("time") == args.time and item.get("area") == args.area
                   and item.get("sha256") == hashes[path]]
        if len(records) != 1:
            raise ValueError("Файл не совпадает с единственной записью реального поля в паспорте: " + variable)
        app.fields_add({"source": args.source, "variable": variable, "level": level,
                        "time": args.time, "area": args.area,
                        "path": str(path)}, local=True)
        app.task.join(120)
        state = app.fields_state()
        assert state["job"]["status"] == "done", state["job"]
        identities[variable] = state["job"]["field_id"]
        verified.append({"variable": variable, "level": level, "filename": path.name,
                         "sha256": hashes[path], "field_id": identities[variable]})
    return identities, verified, provenance


def numerical_check(app, identity, preset, width):
    g = grid(preset, width)
    target = {"crs": g["crs"], "width": g["width"], "height": g["height"],
              "transform": list(g["transform"][:6]),
              "resolution_x_m": g["transform"].a, "resolution_y_m": -g["transform"].e}
    result = app.experimental_pmc_candidates(identity, target_grid=target)
    assert result["candidate_count"] == len(result["candidates"])
    assert all(c["confirmation_status"] == "not_confirmed" for c in result["candidates"])
    assert all(c["geometry"]["status"] in ("complete", "omitted_complexity_limit") for c in result["candidates"])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-netcdf", required=True, help="Existing real u/v 850 hPa NetCDF")
    parser.add_argument("--pressure-netcdf", help="Existing real mean sea-level pressure NetCDF")
    parser.add_argument("--field", action="append", default=[], help="Additional real field: t500=FILE, wind10=FILE, sst=FILE, ice=FILE, lsm=FILE")
    parser.add_argument("--source", choices=("era5", "carra2", "merra2"), required=True)
    parser.add_argument("--time", required=True, help="Exact analysis UTC, e.g. 2020-01-01T00:00:00Z")
    parser.add_argument("--area", type=float, nargs=4, required=True, metavar=("N", "W", "S", "E"))
    parser.add_argument("--provenance", required=True, help="Real-data source/time/area and SHA256 manifest")
    parser.add_argument("--preset", choices=("barents", "arctic", "kara"), default="barents")
    parser.add_argument("--width", type=int, choices=(512, 768, 1024), default=512)
    parser.add_argument("--output", default="browser-results/pmc-workspace")
    parser.add_argument("--executable")
    parser.add_argument("--numerical-only", action="store_true", help="Import and calculate the real fields without a browser")
    args = parser.parse_args()
    output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
    report = {"mode": "real-data-numerical" if args.numerical_only else "real-data-native-http",
              "source": args.source, "valid_time": args.time, "area": args.area,
              "checks": [], "browser_errors": [],
              "network": "localhost only; real inputs fetched separately and verified against provenance.json"}
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        app = Workstation(root / "state", root / "downloads")
        app.era5_credentials({"clear": True})
        # boot() restores this date after its asynchronous /api/state request.
        # Seed it before navigation instead of racing that restore with setDate().
        app.store.set_setting("last_date", args.time[:10])
        server = None
        try:
            identities, report["inputs"], report["provenance"] = install_input_fields(app, args)
            numerical = numerical_check(app, identities["wind"], args.preset, args.width)
            count = numerical["candidate_count"]
            regions = sum(candidate["geometry"]["status"] == "complete" for candidate in numerical["candidates"])
            field_count = len(identities)
            context_count = 1 + int("mslp" in identities)
            source_label = {"era5": "ERA5", "carra2": "CARRA2", "merra2": "MERRA-2"}[args.source]
            analysis_title = f"{source_label} · {args.time[:10]} {args.time[11:16]} UTC · анализ ПМЦ"
            analysis_notes = "Поля реанализа из публичного архива; источник и SHA256 сохранены в паспорте. Кандидаты требуют независимой проверки; ПМЦ не подтверждены."
            report["result"] = numerical
            report["checks"].append({"name": "NetCDF → реальная завихрённость → кандидаты и контуры", "passed": True, "candidate_count": count})
            if args.numerical_only:
                print(json.dumps({"source": args.source, "time": args.time, "candidate_count": count, "fields": len(identities)}, ensure_ascii=False)); return
            from playwright.sync_api import sync_playwright
            server = LocalServer(("127.0.0.1", 0), app)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}"
            page = None
            with sync_playwright() as pw, browser_failure_artifacts(lambda: page, output, report):
                launch = {"headless": True}
                if args.executable: launch["executable_path"] = args.executable
                browser = pw.chromium.launch(**launch)
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
                page = context.new_page()
                page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
                requests = []
                page.on("request", lambda request: requests.append((request.method, request.url)))
                def check(name, expression):
                    report["active_check"] = name
                    page.wait_for_function(expression, timeout=60000)
                    report["checks"].append({"name": name, "passed": True})
                    report.pop("active_check", None)
                    print(name, flush=True)
                page.goto(base + "/#" + server.key)
                check("Запуск с реальными полями и новым рабочим местом ПМЦ", f"()=>S.map&&S.serverState&&S.day==={json.dumps(args.time[:10])}&&FIELDS.fields.length==={field_count}&&document.querySelector('#pmcWorkspacePanel')")
                page.locator("#pmcWorkspaceOpen").click()
                check("Пустое состояние и календарь реального срока доступны", f"()=>S.day==={json.dumps(args.time[:10])}&&document.querySelector('#calendar').children.length>20&&document.querySelector('#pmcCurrentAnalysis').textContent.includes('ещё не открыт')")
                page.locator("#pmcBookmarksOpen").click()
                check("Пустой список закладок объяснён", "()=>document.querySelector('#pmcBookmarkList').textContent.includes('пока нет')")
                page.locator("#pmcBookmarksClose").click()
                page.locator("#pmcPanelAnalysis").click()
                page.locator("#pmcCachedField").select_option(identities["wind"])
                page.locator("#pmcRunCrs").select_option(args.preset)
                page.locator("#pmcRunWidth").select_option(str(args.width))
                page.locator("#pmcRunButton").click()
                check("Численный расчёт совпадает с результатом реального поля и выбирает проекцию", f"()=>PMCW.result?.candidate_count==={count}&&PMCW.result?.source==={json.dumps(args.source)}&&PMCW.result?.valid_time==={json.dumps(args.time)}&&S.map.grid.preset==={json.dumps(args.preset)}&&document.querySelectorAll('#pmcOverlay [data-candidate-region]').length==={regions}")
                page.locator("#pmcRunResult button").filter(has_text="Перейти к карте").click()
                page.locator("#pmcReviewFields summary").click()
                page.locator("#pmcContextMap").click()
                check("Поля того же срока нанесены штатным рендером", f"()=>FIELDS.stack.length==={context_count}&&document.querySelectorAll('#reanalysisLayers>g').length==={context_count}")
                # Export the actual wind-speed raster together with vector layers.
                page.evaluate("async()=>{FIELDS.stack.find(s=>fieldMeta(s.id).variable==='wind').style='fill_arrows';await saveFieldStack();drawFieldStack();await renderFieldMaps();}")
                if "mslp" in identities:
                    check("Изобары видны над заливкой, их легенда без ложной цветовой шкалы", """()=>{
                    const layers=[...document.querySelector('#reanalysisLayers').children],
                      wind=FIELDS.stack.find(s=>fieldMeta(s.id).variable==='wind'),
                      pressure=FIELDS.stack.find(s=>fieldMeta(s.id).variable==='mslp'),
                      windMap=document.getElementById('field-map-'+wind.id),
                      pressureMap=document.getElementById('field-map-'+pressure.id),
                      pressureLegend=document.querySelector('[data-layer="'+pressure.id+'"] .field-legend');
                    return layers.indexOf(windMap)<layers.indexOf(pressureMap)&&pressureMap.querySelector('.field-contour')
                      &&!pressureLegend.querySelector('.field-colorbar')&&pressureLegend.textContent.includes('Изолинии');
                    }""")
                if count:
                    page.locator("#pmcCandidateList button").first.click()
                else:
                    # Zero detections is a valid outcome. Put a manual marker at an
                    # actual sampled wind vector without altering data or thresholds.
                    page.evaluate("""async()=>{const wind=FIELDS.stack.find(s=>fieldMeta(s.id).variable==='wind'),
                        vectors=FIELDS.rendered[wind.id].vectors;
                        if(!vectors.length)throw Error('В области нет доступной точки ветра для ручной пометки.');
                        const point=vectors[Math.floor(vectors.length/2)];await pmcAddAnnotation(point.x,point.y);}""")
                page.locator("#pmcAnnotationStatus").select_option("expert_supported")
                page.locator("#pmcAnnotationForm button[type=submit]").click()
                check("Экспертная оценка требует основания", "()=>document.querySelector('#pmcAnnotationError').textContent.includes('основание')&&PMCW.annotations[0].status==='manual_candidate'")
                page.locator("#pmcAnnotationLabel").fill("Проверка подписи <img src=x onerror=alert(1)>")
                page.locator("#pmcAnnotationNote").fill("Проверка сохранения экспертной пометки. Подтверждение атмосферного случая здесь не выполнялось.")
                page.locator("#pmcAnnotationForm button[type=submit]").click()
                check("Наноска отделена от научного результата и безопасна как текст", "()=>PMCW.annotations[0].status==='expert_supported'&&PMCW.result.candidates.every(candidate=>candidate.confirmation_status==='not_confirmed')&&!document.querySelector('#pmcAnnotations img')&&document.querySelector('#pmcAnnotations').textContent.includes('<img src=x onerror=alert(1)>')")
                page.locator("#pmcAnnotationStatus").select_option("manual_candidate")
                page.locator("#pmcAnnotationLabel").fill(("Кандидат " if count else "Область данных ") + source_label)
                page.locator("#pmcAnnotationNote").fill(
                    "Вихревой кандидат; нужна проверка облачности, морской поверхности, ветра 10 м и SST − T500. ПМЦ не подтверждён."
                    if count else "Реальное поле реанализа. Автоматические кандидаты не найдены; это не доказывает отсутствие ПМЦ.")
                page.locator("#pmcAnnotationForm button[type=submit]").click()
                check("Кнопки панели читаемы, пустая ошибка не занимает места", "()=>[...document.querySelectorAll('#pmcWorkspacePanel .row button')].every(button=>button.scrollWidth<=button.clientWidth+1)&&document.querySelector('#pmcAnnotationError').getBoundingClientRect().height===0")
                page.locator("#pmcWorkspaceTitle").fill(analysis_title)
                page.locator("#pmcWorkspaceNotes").fill(analysis_notes)
                page.locator("#zoomIn").click()
                original_view = page.evaluate("S.view")
                page.evaluate("""()=>{const prototype=CanvasRenderingContext2D.prototype;
                    window.__pmcGradient=prototype.createLinearGradient;window.__pmcFillText=prototype.fillText;
                    window.__pmcLegendGradients=0;window.__pmcExportText=[];
                    prototype.createLinearGradient=function(...args){window.__pmcLegendGradients++;return window.__pmcGradient.apply(this,args);};
                    prototype.fillText=function(...args){window.__pmcExportText.push(String(args[0]));return window.__pmcFillText.apply(this,args);};
                }""")
                with page.expect_download() as download_info:
                    page.locator("#pmcPngExport").click()
                downloaded = download_info.value
                png_path = output / f"01-{args.source}-analysis.png"
                downloaded.save_as(png_path)
                assert png_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
                from PIL import Image
                with Image.open(png_path) as image:
                    assert image.width == 1600 and image.height > 1000
                    assert len(image.convert("RGB").getcolors(image.width*image.height)) > 100
                assert page.evaluate("window.__pmcLegendGradients===1"), "PNG must show a gradient only for the wind fill"
                if "mslp" in identities:
                    assert page.evaluate("window.__pmcExportText.some(text=>text.startsWith('Изолинии '))"), "PNG must distinguish pressure contours from the wind fill"
                page.evaluate("()=>{CanvasRenderingContext2D.prototype.createLinearGradient=window.__pmcGradient;CanvasRenderingContext2D.prototype.fillText=window.__pmcFillText;}")
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
                page.locator("#pmcWorkspaceNotes").fill(analysis_notes)
                page.locator("#pmcBookmarkSave").click()
                check("Закладка сохраняет реальный анализ без экспертного подтверждения", "()=>!PMCW.dirty&&PMCW.annotations[0].status==='manual_candidate'")
                page.screenshot(path=str(output / "02-workspace-desktop.png"))
                before = len([url for method, url in requests if method == "POST" and url.endswith("/api/pmc/run")])
                page.reload()
                check("Повторная загрузка приложения завершена", f"()=>S.map&&FIELDS.fields.length==={field_count}")
                page.locator("#pmcWorkspaceOpen").click()
                page.locator("#pmcBookmarksOpen").click()
                page.locator("#pmcBookmarkList button").filter(has_text="Открыть анализ").click()
                check("Закладка восстанавливает реальный результат и ручную пометку", f"()=>PMCW.restored&&PMCW.annotations.length===1&&PMCW.annotations[0].status==='manual_candidate'&&document.querySelectorAll('#pmcOverlay [data-candidate-region]').length==={regions}&&FIELDS.stack.length==={context_count}")
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
