#!/usr/bin/env python3
"""Проверка пользовательских сценариев на явно синтетическом наборе.

Требуется Playwright только для разработки. По умолчанию настоящий HTTP/cookies.
--bridge предназначен для среды, в которой браузеру запрещён localhost:
перехватывает запросы к arktika.test и передаёт их тому же HTTP-серверу через Python.
Такой запуск не проверяет браузерную защиту Origin; отдельные API-тесты проверяют её.
"""
from __future__ import annotations
import argparse
import json
import os
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
from arktika.workstation import Workstation
from test_science import fixture
from test_interpretation import profile
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bridge', action='store_true')
    parser.add_argument('--executable')
    parser.add_argument('--output', default='browser-results')
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    report = {'mode': 'explicit-http-bridge' if args.bridge else 'native-http',
              'fixture': 'SYNTHETIC SOFTWARE TEST; NOT WEATHER OBSERVATIONS',
              'checks': [], 'browser_errors': []}
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
                if args.bridge:
                    import base64
                    import re
                    def bridge(payload):
                        headers = {'Cookie': 'arktika_session=' + server.key, 'Origin': base,
                                   'Content-Type': 'application/json', 'X-Arktika-Request': '1'}
                        req = urllib.request.Request(base + payload['path'], headers=headers,
                            data=payload.get('body', '').encode() if payload.get('body') is not None else None,
                            method=payload.get('method', 'GET'))
                        try:
                            response = proxy.open(req, timeout=30)
                        except urllib.error.HTTPError as exc:
                            response = exc
                        with response:
                            return {'status': response.status, 'headers': dict(response.headers),
                                    'body': base64.b64encode(response.read()).decode()}
                    page.expose_function('httpBridge', bridge)
                    html = (ROOT / 'static/index.html').read_text()
                    html = re.sub(r'<script[^>]+src=[^>]+></script>', '', html)
                    html = re.sub(r'<link[^>]+>', '', html)
                    html = html.replace('</head>', '<style>' + (ROOT / 'static/style.css').read_text() + '</style></head>')
                    page.set_content(html)
                    page.evaluate(r"""() => {
                        window.fetch = async (path, options={}) => {
                            const r = await window.httpBridge({path: String(path), method: options.method || 'GET', body: options.body ?? null});
                            const bytes = Uint8Array.from(atob(r.body), c=>c.charCodeAt(0));
                            return new Response(bytes, {status:r.status, headers:r.headers});
                        };
                        const setter=Element.prototype.setAttribute;
                        Element.prototype.setAttribute=function(name,value){
                            if((name==='href'||name==='src') && (this.tagName==='image'||this.tagName==='IMG') && String(value).startsWith('/')){
                                const wanted=String(value);this.__wantedSource=wanted;
                                fetch(wanted).then(r=>r.blob()).then(b=>{if(this.__wantedSource===wanted)setter.call(this,name,URL.createObjectURL(b));});
                            }else setter.call(this,name,value);
                        };
                    }""")
                    page.add_script_tag(content=(ROOT / 'static/app.js').read_text())
                    page.add_script_tag(content=(ROOT / 'static/studio.js').read_text())
                else:
                    page.goto(base + '/#' + server.key)
                def wait_js(expression, arg=None, timeout=30000):
                    """Poll through Runtime.evaluate so the smoke test respects the app CSP."""
                    deadline = time.monotonic() + timeout / 1000
                    last_error = None
                    while time.monotonic() < deadline:
                        try:
                            ok = page.evaluate(expression, arg) if arg is not None else page.evaluate(expression)
                            if ok:
                                return
                        except Exception as exc:
                            last_error = exc
                        page.wait_for_timeout(100)
                    detail = f": {last_error}" if last_error else ""
                    raise TimeoutError("Не выполнено условие браузерной проверки" + detail)

                wait_js('() => typeof S !== "undefined" && S.scenes.length > 0')
                def check(name, action):
                    action()
                    report['checks'].append({'name': name, 'passed': True})
                def loaded(product=None):
                    wait_js('(p) => S.product && (!p || S.product.product === p) && !S.busy && !UI.activeBuild', product)
                check('Начальная дата и локальный сеанс', lambda: page.locator('#sessions button.session').first.click())
                loaded('channel')
                check('Канал 9 не объявлен температурой без калибровки',
                      lambda: wait_js('() => [...document.querySelectorAll("#taskGrid .task-card")].some(b => b.textContent.includes("Оконный ИК"))'))
                check('Некалиброванный канал остаётся DN', lambda: wait_js('() => S.product.legend.units === "DN"'))
                check('Легенда открывается', lambda: page.locator('#legendOpen').click())
                page.screenshot(path=str(out / 'channel.png'))
                page.locator('#calibrationOpen').click()
                page.locator('#calMode').select_option('assumed')
                page.locator('#saveCalibration').click()
                page.locator('#confirmYes').click()
                wait_js('() => S.product && S.product.calibration_status === "assumed" && !S.busy')
                check('Допущение видно в продукте', lambda: wait_js('() => S.product.calibration_status === "assumed"'))
                page.locator('#productRail').click()
                page.locator('#product').select_option('micro24')
                loaded('micro24')
                check('Цветосинтез и согласованная легенда', lambda: wait_js('() => S.product.legend.interpretation.swatches.length > 0'))
                page.screenshot(path=str(out / 'microphysics.png'))
                # Координаты преобразует production API, а щелчок выполняет браузер.
                def click_point(lon=30, lat=70):
                    xy = page.evaluate('async p => (await api("/api/project-points", {product:S.product.id,points:[p]})).points[0]', [lon, lat])
                    screen = page.evaluate('p=>{const q=new DOMPoint(...p).matrixTransform(document.querySelector("#map").getScreenCTM());return [q.x,q.y];}', xy)
                    page.mouse.click(*screen)
                    wait_js('() => UI.analysis !== null', timeout=15000)
                check('Щелчок по карте и анализ исходного пикселя', click_point)
                check('Экспорт точки доступен', lambda: page.locator('#analysisExport').wait_for(state='visible'))
                page.screenshot(path=str(out / 'point.png'))
                page.locator('#profilesOpen').click()
                page.locator('#profileImportDetails').evaluate('(e)=>e.open=true')
                naive_path = data / 'synthetic-profile-naive-time.json'
                naive = profile()
                naive['source'] = 'SYNTHETIC BROWSER TEST; NOT OBSERVATIONS'
                naive['valid_time'] = '2026-01-01T00:00:23'
                naive_path.write_text(json.dumps(naive), encoding='utf-8')
                page.locator('#profileFile').set_input_files(str(naive_path))
                wait_js('() => UI.profileText.length > 0')
                check('Профиль без часового пояса не подменяется локальным временем',
                      lambda: wait_js('() => document.querySelector("#profileTime").value === ""'))
                path = data / 'synthetic-profile.json'
                prof = profile()
                prof['source'] = 'SYNTHETIC BROWSER TEST; NOT OBSERVATIONS'
                prof['valid_time'] = '2026-01-01T00:00:23Z'
                path.write_text(json.dumps(prof), encoding='utf-8')
                page.locator('#profileFile').set_input_files(str(path))
                wait_js('() => UI.profileText.length > 0')
                page.locator('#importProfile').click()
                wait_js('() => UI.profiles.length === 1')
                check('JSON-профиль сохраняет секунды срока', lambda: wait_js('() => UI.profiles[0].valid_time.endsWith("00:00:23Z")'))
                if page.locator('#profilesDialog').evaluate('(e)=>e.open'):
                    page.locator('#profilesDialog [data-close]').click()
                page.locator('#routeRail').click()
                page.locator('#routePoints').evaluate('(e)=>e.closest("details").open=true')
                page.locator('#routePoints').fill('30, 70\n31, 71')
                page.locator('#routePoints').dispatch_event('change')
                page.locator('#departure').fill('2026-01-01T00:00')
                page.locator('#departure').dispatch_event('change')
                page.locator('#calculateRoute').click()
                wait_js('() => S.route !== null', timeout=15000)
                check('Расчёт маршрута', lambda: wait_js('() => S.route.samples.length > 1'))
                page.screenshot(path=str(out / 'route.png'))
                page.locator('#clearRoute').click()
                check('Очистка не оставляет старые значения', lambda: wait_js('() => S.route === null && document.querySelector("#routeResult").childElementCount === 0'))
                page.locator('#dateOpen').click()
                page.locator('#date').fill('2025-12-31')
                page.locator('#date').dispatch_event('change')
                check('Новая дата сбрасывает анализ и экспорт', lambda: wait_js('() => UI.analysis===null && document.querySelector("#analysisExport").hidden'))
                page.locator('#date').fill('2026-01-01')
                page.locator('#date').dispatch_event('change')
                wait_js('() => S.scenes.length > 0')
                page.locator('#sessions button.session').first.click()
                loaded()
                page.locator('#productRail').click()
                page.locator('#product').select_option('difference')
                page.locator('#product').select_option('phase')
                loaded('phase')
                check('Быстрая смена продуктов оставляет последний выбор', lambda: wait_js('() => S.product.product === document.querySelector("#product").value'))
                context.new_page().close()
                for width in (1024, 390):
                    page.set_viewport_size({'width': width, 'height': 844})
                    page.wait_for_timeout(200)
                    check('Нет переполнения при ширине '+str(width), lambda: wait_js('() => document.documentElement.scrollWidth <= innerWidth + 1'))
                    page.screenshot(path=str(out / ('width-'+str(width)+'.png')))
                assert not report['browser_errors'], report['browser_errors']
                browser.close()
        except Exception as exc:
            report['error'] = str(exc).replace(server.key, '[LOCAL SESSION]')
            try:
                report['dom_status'] = page.locator('#status').inner_text()
                page.screenshot(path=str(out / 'failure.png'))
            except Exception:
                pass
            raise
        finally:
            (out / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            server.shutdown(); thread.join(5); server.server_close(); app.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
