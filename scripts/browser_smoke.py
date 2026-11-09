#!/usr/bin/env python3
"""Техническая сцена для UI и обязательный реальный профиль из внешнего файла.

Синтетический профиль используется только для проверки отказа при импорте.
Сценарий не является метеорологической верификацией спутниковых алгоритмов.

По умолчанию настоящий localhost, cookies и CSP. --bridge нужен только для
изолированных сред и не проверяет браузерную защиту Origin.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from server import LocalServer
from browser_support import mount
from arktika.workstation import Workstation
from arktika.interpretation import parse_profile, utc
from arktika.source_validation import reject_explicit_synthetic_metadata
from test_science import fixture
from test_interpretation import profile
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bridge', action='store_true')
    parser.add_argument('--executable')
    parser.add_argument('--output', default='browser-results')
    parser.add_argument('--real-profile', type=Path, required=True,
                        help='JSON реального профиля с исходными source, valid_time, lat/lon и уровнями')
    args = parser.parse_args()
    real_path = args.real_profile.expanduser().resolve(strict=True)
    real_bytes = real_path.read_bytes()
    if len(real_bytes) > 1_000_000:
        parser.error('Реальный профиль должен содержать не более 1 МБ.')
    real_input = json.loads(real_bytes.decode('utf-8-sig'))
    reject_explicit_synthetic_metadata(real_input)
    real_profile = parse_profile(real_input)
    profile_gap_hours = abs((utc(real_profile['valid_time']) - utc('2026-01-01T00:00:00Z')).total_seconds())/3600
    if profile_gap_hours <= real_profile['max_hours']:
        parser.error('Для проверки временной неприменимости нужен реальный профиль вне допуска к технической сцене 2026-01-01 00:00 UTC. Время файла не изменяется.')
    expected_profile = {key: real_profile[key] for key in ('source', 'valid_time', 'lat', 'lon')}
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    source_names = ['static/studio.js', 'scripts/browser_smoke.py', 'docs/QUICKSTART.html', 'docs/profile-template.csv']
    report = {'mode': 'explicit-http-bridge' if args.bridge else 'native-http',
              'fixture': 'SYNTHETIC SOFTWARE TEST; NOT WEATHER OBSERVATIONS',
              'real_profile': dict(expected_profile, sha256=hashlib.sha256(real_bytes).hexdigest(),
                                   filename=real_path.name, levels=len(real_profile['height']),
                                   fixture_time_gap_hours=profile_gap_hours),
              'source_sha256': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in source_names},
              'checks': [], 'browser_errors': []}
    print(json.dumps({'source_sha256': report['source_sha256']}), flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        fixture(data)
        app = Workstation(data / 'state')
        app.scan_local(data)
        app.store.set_setting('last_date', '2026-01-01')
        app.store.set_setting('view_settings', {'preset': 'barents', 'product': 'channel', 'channel': 9})
        server = LocalServer(('127.0.0.1', 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with sync_playwright() as pw:
                launch = {'headless': True}
                if args.executable:
                    launch['executable_path'] = args.executable
                browser = pw.chromium.launch(**launch)
                context = browser.new_context(viewport={'width': 1536, 'height': 960})
                page = context.new_page()
                page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))
                mount(page,ROOT,server,args.bridge)

                def wait_js(expression, arg=None, timeout=30000):
                    deadline = time.monotonic() + timeout/1000
                    last_error = None
                    while time.monotonic() < deadline:
                        try:
                            ok = page.evaluate(expression, arg) if arg is not None else page.evaluate(expression)
                            if ok:
                                return
                        except Exception as exc:
                            last_error = exc
                        page.wait_for_timeout(100)
                    raise TimeoutError('Не выполнено условие: ' + expression + (f': {last_error}' if last_error else ''))

                def check(name, expression, arg=None):
                    wait_js(expression, arg)
                    report['checks'].append({'name': name, 'passed': True})

                def loaded(product=None, channel=None):
                    wait_js('(p)=>S.product&&(!p.product||S.product.product===p.product)&&(!p.channel||Number(S.product.request.channel)===p.channel)&&!S.busy&&!UI.activeBuild', {'product':product,'channel':channel})

                def screenshot(name):
                    page.screenshot(path=str(out/name))

                def coordinates(text):
                    page.locator('#routePoints').evaluate('(e)=>e.closest("details").open=true')
                    page.locator('#routePoints').fill(text)
                    page.locator('#routePoints').dispatch_event('change')
                    wait_js('()=>document.querySelectorAll("#routeLayer circle").length===2')

                def choose_profile(path):
                    page.locator('#profileFile').set_input_files(str(path))
                    wait_js('(name)=>document.querySelector("#profileFileName").textContent===name', path.name)

                wait_js('()=>typeof S!=="undefined"&&S.scenes.length>0')
                page.locator('#authOpen').click()
                page.locator('#sourcesDialog').wait_for(state='visible')
                check('Единый реестр показывает GPTL, ERA5, CARRA2 и MERRA-2', '()=>["Арктика-М / GPTL","ERA5","CARRA2","MERRA-2"].every(name=>document.querySelector("#sourceRows").textContent.includes(name))')
                check('ERA5 и CARRA2 показывают общий CDS-доступ', '()=>document.querySelectorAll("#sourceRows [data-source=era5],#sourceRows [data-source=carra2]").length===2&&document.querySelector("#sourceRows").textContent.includes("Copernicus CDS")')
                page.locator('#sourcesDialog [data-close]').click()
                page.locator('#sessions button.session').first.click()
                loaded('channel', 9)
                check('Начальная дата и локальный сеанс', '()=>S.day==="2026-01-01"&&S.scene!==null')
                check('Оконный ИК не объявлен температурой без калибровки', '()=>[...document.querySelectorAll("#taskGrid .task-card")].some(b=>b.textContent.includes("Оконный ИК"))&&S.product.legend.units==="DN"')
                check('Без профиля высоты скрыты', '()=>document.querySelector("#pointProfileFields").hidden&&document.querySelector("#routeHeightFields").hidden')
                check('Подписи карточек на отдельных строках', '()=>[...document.querySelectorAll(".task-card small")].every(e=>getComputedStyle(e).display==="block")')
                page.locator('#legendOpen').click()
                check('Легенда различает шкалу и диапазон данных', '()=>document.querySelector("#legend").textContent.includes("Цветовая шкала")&&document.querySelector("#legend").textContent.includes("Диапазон данных")')
                screenshot('channel.png')
                if not args.bridge:
                    with page.expect_popup() as popup_info:
                        page.locator('#legend .export-link').click()
                    popup = popup_info.value
                    popup.wait_for_url('**/docs/QUICKSTART.html#signal')
                    popup.wait_for_load_state('domcontentloaded')
                    assert popup.url.endswith('/docs/QUICKSTART.html#signal')
                    assert popup.locator('h1').inner_text() == '🛰️ Рабочее место метеоролога «Арктика-М»'
                    report['checks'].append({'name':'Ссылка легенды открывает HTML и нужный раздел','passed':True})
                    popup.close()
                check('UI не содержит ссылок на исходный Markdown', r'()=>![...document.querySelectorAll("a[href]")].some(a=>/\.md(?:#.*)?$/.test(a.getAttribute("href")))')
                check('Шаблон CSV доступен и не содержит вымышленных наблюдений', 'async()=>{const r=await fetch("/docs/profile-template.csv");return r.ok&&(await r.text()).trim()==="height_m,temperature_c"}')
                page.locator('#productQuick').click()
                page.locator('#channel').select_option('4')
                loaded('channel',4)
                page.locator('#routeRail').click()
                check('Расчёт без двух точек недоступен', '()=>document.querySelector("#calculateRoute").disabled')
                coordinates('30, 70\n31, 71')
                page.locator('#calculateRoute').click()
                check('Маршрут без профиля и ETA', '()=>S.route!==null&&document.querySelectorAll("#routeResult th").length===2')
                check('График и таблица показывают канал 4 в DN', '()=>document.querySelector("#routeResult th:nth-child(2)").textContent==="Канал 4, DN"&&document.querySelector("#routeResult svg").textContent.includes("Канал 4 · DN")')
                page.locator('#routeChannel').select_option('9')
                check('Смена канала синхронно меняет таблицу', '()=>document.querySelector("#routeResult th:nth-child(2)").textContent==="Канал 9, DN"')
                page.locator('#routeChannel').select_option('4')
                screenshot('route-dn.png')
                before = page.locator('#routeLayer circle').first.bounding_box()['width']
                before_text = page.locator('#routeLayer text').first.bounding_box()['height']
                page.evaluate('()=>{const p=document.querySelector("#routeLayer circle");zoom(.2,Number(p.getAttribute("cx")),Number(p.getAttribute("cy")));}')
                page.wait_for_timeout(250)
                after = page.locator('#routeLayer circle').first.bounding_box()['width']
                after_text = page.locator('#routeLayer text').first.bounding_box()['height']
                assert abs(before-after)<1 and abs(before_text-after_text)<1,(before,after,before_text,after_text)
                report['checks'].append({'name':'Маркеры и подписи не растут при пятикратном увеличении карты','passed':True})
                screenshot('map-zoom.png')
                page.locator('#mapReset').click()
                check('Пропуск разрывает график, не становится нулём', '''()=>{
                    const r={length_km:4,units:{4:{unit:'DN'}},samples:[1,2,null,3,4].map((v,i)=>({distance_km:i,channels:{4:v}}))};
                    const node=document.createElement('div');node.innerHTML=routeChart(r,4);
                    return node.querySelectorAll('polyline').length===2&&routeValue(r.samples[2],'4',r.units)===null;
                }''')
                page.locator('#routeOptions > summary').click()
                page.locator('#routeTimingEnabled').check()
                page.locator('#departure').fill('2026-01-01T00:00')
                page.locator('#calculateRoute').click()
                check('ETA включается только по явному выбору', '()=>S.route!==null&&document.querySelectorAll("#routeResult th").length===3&&S.route.samples[0].eta!==""')
                page.locator('#routeTimingEnabled').uncheck()
                check('Изменение параметров убирает старый экспорт', '()=>S.route===null&&document.querySelector("#routeResult").childElementCount===0')
                page.locator('#clearRoute').click()
                check('Очистка маршрута', '()=>S.route===null&&document.querySelector("#routeLayer").childElementCount===0')
                page.locator('#profilesOpen').click()
                page.locator('#profilesDialog').wait_for(state='visible')
                check('Импорт начинается с файла, не с шести обязательных полей', '()=>document.querySelector("#profileMetadata").hidden&&document.querySelector("#importProfile").disabled')
                screenshot('profile-empty.png')
                naive_path = data/'synthetic-profile-naive-time.json'
                naive=profile();naive['source']='SYNTHETIC BROWSER TEST; NOT OBSERVATIONS';naive['valid_time']='2026-01-01T00:00:23'
                naive_path.write_text(json.dumps(naive),encoding='utf-8')
                choose_profile(naive_path)
                check('Время без часового пояса не угадывается', '()=>document.querySelector("#profileTime").value===""&&document.querySelector("#importProfile").disabled')
                synthetic_path=data/'synthetic-profile.json'
                prof=profile();prof['source']='SYNTHETIC BROWSER TEST; NOT OBSERVATIONS';prof['valid_time']='2026-01-01T03:00:23+03:00'
                synthetic_path.write_text(json.dumps(prof),encoding='utf-8')
                choose_profile(synthetic_path)
                check('Форма распознаёт UTC и секунды отрицательного примера', '()=>document.querySelector("#profileTime").value==="2026-01-01T00:00:23"&&!document.querySelector("#importProfile").disabled')
                page.locator('#importProfile').click()
                check('Синтетический профиль отклоняется без сохранения и выбора', '()=>!UI.profileImportBusy&&UI.profiles.length===0&&!document.querySelector("#profileImportError").hidden&&document.querySelector("#profileImportError").textContent.includes("Синтетические")&&document.querySelector("#routeProfile").value===""&&document.querySelector("#analysisProfile").value===""')
                choose_profile(real_path)
                check('Реальный JSON сохраняет исходный UTC, источник и координаты', '''p=>{
                    const value=document.querySelector('#profileTime').value;
                    return new Date(value+'Z').getTime()===new Date(p.valid_time).getTime()
                        && document.querySelector('#profileSource').value===p.source
                        && Number(document.querySelector('#profileLat').value)===p.lat
                        && Number(document.querySelector('#profileLon').value)===p.lon
                        && !document.querySelector('#profileMetadata').open
                        && !document.querySelector('#importProfile').disabled;
                }''', expected_profile)
                screenshot('profile-ready.png')
                broken=data/'broken.json';broken.write_text('{bad',encoding='utf-8')
                page.locator('#profileFile').set_input_files(str(broken))
                check('Неверный файл снимает предыдущий импорт', '()=>UI.profileText===""&&document.querySelector("#importProfile").disabled&&!document.querySelector("#profileImportError").hidden')
                choose_profile(real_path)
                page.locator('#importProfile').click()
                check('Реальный профиль сохранён со своим сроком и выбран для маршрута', 'p=>UI.profiles.length===1&&!UI.profileImportBusy&&UI.profiles[0].valid_time===p.valid_time&&UI.profiles[0].source===p.source&&document.querySelector("#routeProfile").value===UI.profiles[0].id&&document.querySelector("#analysisProfile").value===""', expected_profile)
                saved_profile = app.get_profile(page.evaluate('UI.profiles[0].id'))
                for key in ('source', 'valid_time', 'lat', 'lon', 'height', 'temperature', 'pressure'):
                    if key in real_profile:
                        assert saved_profile[key] == real_profile[key], 'Импорт изменил '+key+' реального профиля'
                assert real_path.read_bytes() == real_bytes, 'Исходный профиль изменился во время проверки'
                report['checks'].append({'name': 'Уровни и фактические метаданные реального профиля сохранены без подмены', 'passed': True})
                page.locator('#routeProfile').select_option('')
                page.locator('#calibrationOpen').click()
                page.locator('#calMode').select_option('assumed')
                page.locator('#scaleAll').click()
                page.locator('#saveCalibration').click();page.locator('#confirmYes').click()
                wait_js('()=>S.product&&S.product.calibration_status==="assumed"&&!S.busy&&!UI.activeBuild')
                check('Исследовательское допущение сохранено', '()=>S.product.calibration_status==="assumed"')
                page.locator('#productQuick').click()
                page.locator('#product').select_option('micro24');loaded('micro24')
                check('RGB и легенда согласованы', '()=>S.product.legend.interpretation.swatches.length>0')
                screenshot('microphysics.png')
                xy=page.evaluate('async()=> (await api("/api/project-points",{product:S.product.id,points:[[30,70]]})).points[0]')
                screen=page.evaluate('p=>{const q=new DOMPoint(...p).matrixTransform(document.querySelector("#map").getScreenCTM());return [q.x,q.y];}',xy)
                page.mouse.click(*screen)
                check('Щелчок анализирует исходный пиксель и даёт экспорт', '()=>UI.analysis!==null&&!document.querySelector("#analysisExport").hidden')
                screenshot('point.png')
                page.locator('#analysisProfile').select_option(page.evaluate('UI.profiles[0].id'))
                check('Выбор профиля точки не меняет профиль маршрута', '()=>UI.analysis?.profile_result!==undefined&&document.querySelector("#routeProfile").value===""')
                check('Реальный профиль другого срока не используется для ВГО и слоя', 'p=>UI.analysis?.profile_snapshot?.valid_time===p.valid_time&&UI.analysis.profile_result.profile.applicable===false&&UI.analysis.profile_result.height.status==="outside"&&UI.analysis.profile_result.layer.status==="outside"&&!UI.analysis.profile_result.candidate_cloud_top', expected_profile)
                page.locator('#routeRail').click()
                coordinates('30,70\n31,71')
                page.locator('#calculateRoute').click()
                check('Калиброванный маршрут показывает °C', '()=>S.route!==null&&document.querySelector("#routeResult th:nth-child(2)").textContent.includes("°C")')
                screenshot('route.png')
                page.locator('#routePoints').fill('wrong coordinates')
                check('Ошибка ввода немедленно скрывает старый результат', '()=>S.route===null&&document.querySelector("#routeResult").childElementCount===0&&document.querySelector("#calculateRoute").disabled')
                page.locator('#clearRoute').click()
                page.locator('#dateOpen').click()
                page.locator('#date').fill('2025-12-31');page.locator('#date').dispatch_event('change')
                check('Смена даты сбрасывает точку и экспорт', '()=>UI.analysis===null&&document.querySelector("#analysisExport").hidden')
                page.locator('#date').fill('2026-01-01');page.locator('#date').dispatch_event('change')
                wait_js('()=>S.scenes.length>0');page.locator('#sessions button.session').first.click();loaded()
                page.locator('#productQuick').click()
                page.locator('#product').select_option('difference');page.locator('#product').select_option('phase');loaded('phase')
                check('Быстрая смена продуктов оставляет последний выбор', '()=>S.product.product===document.querySelector("#product").value')
                for width in (1024,390):
                    page.set_viewport_size({'width':width,'height':844});page.wait_for_timeout(200)
                    check('Нет переполнения окна '+str(width), '()=>document.documentElement.scrollWidth<=innerWidth+1')
                    screenshot('width-'+str(width)+'.png')
                assert not report['browser_errors'],report['browser_errors']
                browser.close()
        except Exception as exc:
            report['error']=str(exc).replace(server.key,'[LOCAL SESSION]')
            try:
                report['dom_status']=page.locator('#status').inner_text()
                page.screenshot(path=str(out/'failure.png'))
            except Exception:
                pass
            raise
        finally:
            (out/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            server.shutdown();thread.join(5);server.server_close();app.close()
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
