#!/usr/bin/env python3
"""Дополнительная проверка каталога в настоящем Workstation, без загрузок GPTL.

Синтетические GeoTIFF и метаданные не являются наблюдениями. Требуется Playwright.
Скрипт включён для приёмки после применения пакета; в среде подготовки не выполнен.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from playwright.sync_api import sync_playwright
from arktika.workstation import Workstation
from server import LocalServer
from test_science import fixture
from test_catalog_integration import remote_fixture


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='catalog-results');parser.add_argument('--executable')
    args=parser.parse_args();output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    report=dict(mode='native-http-workstation',fixture='SYNTHETIC SOFTWARE TEST, NOT OBSERVATIONS',checks=[],browser_errors=[])
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);source=root/'local';source.mkdir();fixture(source)
        app=Workstation(root/'state');app.scan_local(source);app.store.set_setting('last_date','2026-01-01')
        app.store.set_setting('view_settings',dict(preset='barents',product='channel',channel=9))
        record,assets=remote_fixture();app.store.upsert([record],assets)
        server=LocalServer(('127.0.0.1',0),app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as pw:
                options=dict(headless=True)
                if args.executable:options['executable_path']=args.executable
                browser=pw.chromium.launch(**options);context=browser.new_context(viewport=dict(width=1365,height=900));page=context.new_page()
                page.on('pageerror',lambda error:report['browser_errors'].append(str(error)))
                def wait(expression):
                    deadline=time.monotonic()+30
                    while time.monotonic()<deadline:
                        if page.evaluate(expression):return
                        page.wait_for_timeout(100)
                    raise TimeoutError(expression)
                def check(name,expression):
                    wait(expression);report['checks'].append(dict(name=name,passed=True))
                page.goto('http://127.0.0.1:'+str(server.server_port)+'/#'+server.key)
                check('Оба срока в общем списке','()=>typeof CATALOG!=="undefined"&&CATALOG.rows.length===2')
                page.locator('#catalogTask').select_option('micro24')
                check('Комплектность по продукту','()=>catalogPlan(CATALOG.rows.find(s=>s.local_id)).ready')
                page.locator('#catalogAvailability').select_option('ready')
                check('Готовый локальный срок','()=>document.querySelectorAll(".catalog-card").length===1')
                page.locator('#catalogTask').select_option('channel')
                page.locator('#catalogChannel').select_option('4')
                page.locator('#sessions button.session').click()
                check('Один щелчок открывает выбранный канал','()=>S.product&&S.product.product==="channel"&&Number(S.product.request.channel)===4&&!S.busy&&!UI.activeBuild')
                check('DN не подменены температурой','()=>S.product.legend.units==="DN"')
                page.locator('#catalogAvailability').select_option('all');page.locator('#catalogSearch').fill('01:00')
                check('Поиск удалённого срока','()=>document.querySelectorAll(".catalog-card").length===1')
                page.locator('#catalogTask').select_option('micro24');page.locator('.catalog-files').click()
                check('Список файлов получен через API','()=>S.fileAssets.length===3')
                page.locator('#selectChannels').click()
                check('Подобран набор без лишних файлов','()=>S.selection.size===3')
                page.locator('#filesDialog [data-close]').click()
                page.screenshot(path=str(output/'catalog-desktop.png'))
                page.set_viewport_size(dict(width=390,height=844));page.wait_for_timeout(150)
                check('Ширина 390 без переполнения','()=>document.documentElement.scrollWidth<=innerWidth+1')
                page.screenshot(path=str(output/'catalog-mobile.png'))
                if report['browser_errors']:raise AssertionError(report['browser_errors'])
                browser.close()
        except Exception as exc:
            report['error']=str(exc).replace(server.key,'[LOCAL SESSION]');raise
        finally:
            (output/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            server.shutdown();thread.join(5);server.server_close();app.close()
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
