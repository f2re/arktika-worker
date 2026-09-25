#!/usr/bin/env python3
"""Приёмка архивных RGB и прокрутки окон. Только синтетические тестовые данные."""
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
from arktika.auth import AuthClient
from arktika.workstation import Workstation
from server import LocalServer
from test_archive import rgb_fixture,archive_assets,RGBTransport,STAMP
from test_science import fixture
from browser_support import mount


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='browser-results/archive')
    parser.add_argument('--executable');parser.add_argument('--bridge',action='store_true');args=parser.parse_args()
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    report=dict(mode='explicit-http-bridge' if args.bridge else 'native-http',fixture='SYNTHETIC SOFTWARE TEST, NOT OBSERVATIONS',checks=[],browser_errors=[])
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);path=root/'source.tif';rgb_fixture(path,4326)
        transport=RGBTransport(path.read_bytes());app=Workstation(root/'state',root/'downloads',client=AuthClient(transport=transport))
        record,assets=archive_assets();app.store.upsert([record],assets)
        known=next(a for a in assets if a['epsg']==3857);stored=root/known['filename'];rgb_fixture(stored,3857,tags=False)
        app.store.enqueue(known,stored);app.store.update_job(known['id'],state='done',done=stored.stat().st_size)
        normal=root/'normal';normal.mkdir();fixture(normal);app.scan_local(normal)
        app.store.set_setting('last_date','2024-09-20');app.store.set_setting('view_settings',dict(preset='barents',product='channel',channel=9))
        server=LocalServer(('127.0.0.1',0),app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as pw:
                options=dict(headless=True)
                if args.executable:options['executable_path']=args.executable
                browser=pw.chromium.launch(**options);page=browser.new_page(viewport=dict(width=1440,height=960))
                page.on('pageerror',lambda e:report['browser_errors'].append(str(e)))
                def wait(expression,timeout=30):
                    end=time.monotonic()+timeout
                    while time.monotonic()<end:
                        try:
                            if page.evaluate(expression):return
                        except Exception:pass
                        page.wait_for_timeout(100)
                    raise AssertionError(expression)
                def check(name,expression):wait(expression);report['checks'].append(dict(name=name,passed=True))
                def open_queue():
                    # Щелчок запускает HTTP-запрос; прокручивать ещё скрытое окно нельзя.
                    page.locator('#queueOpen').click()
                    page.locator('#queueDialog').wait_for(state='visible')
                    wait('()=>{const e=document.querySelector("#queueDialog .dialog-body");return e.clientHeight>0&&e.scrollHeight>e.clientHeight;}')
                try:
                    mount(page,ROOT,server,args.bridge)
                    check('Старый скачанный RGB открывается без повторной загрузки','()=>typeof S!=="undefined"&&S.product?.display_only&&!S.busy&&!UI.activeBuild')
                    assert transport.calls==0
                    check('На карте нужный архивный срок и проекция','()=>S.product.time==="2024-09-20T19:00:00Z"&&S.product.legend.source_crs==="EPSG:3857"')
                    check('Нет ложного требования десяти каналов','()=>document.querySelector("#sessions button.session").textContent==="Открыть RGB"&&!document.querySelector("#sessions .catalog-channels")')
                    page.locator('#catalogTask').select_option('all')
                    check('Готовый RGB доступен и при выборе общего набора','()=>document.querySelector("#sessions button.session").textContent==="Открыть RGB"&&!document.querySelector("#sessions button.session").disabled')
                    page.locator('#legendOpen').click()
                    check('Отдельная легенда без температуры и фаз','()=>document.querySelector("#legend").textContent.includes("Цвета сохранены")&&!document.querySelector("#legend .colorbar")')
                    page.screenshot(path=str(out/'archive-map.png'))
                    page.locator('.catalog-files').click()
                    check('Скачанный вариант имеет действие «На карту»','()=>document.querySelectorAll(".open-composite").length===1')
                    page.locator('.queue-composite').click()
                    wanted=next(a for a in assets if a['epsg']==4326)['id']
                    check('Скачать и открыть: настоящая очередь и обработчик','()=>S.product?.request.composite==='+json.dumps(wanted)+'&&!S.busy&&!UI.activeBuild')
                    assert transport.calls>0
                    check('RGB не подменён исходными каналами','()=>S.scene.channels.length===0&&S.product.legend.units==="RGB"')
                    page.evaluate('()=>inspectAt(100,100)')
                    check('Анализ RGB не предлагает выдуманную калибровку','()=>document.querySelector("#pointControls").hidden&&document.querySelector("#pixel").textContent.includes("нет исходных значений")')
                    page.locator('#routeRail').click();page.evaluate("()=>{document.querySelector('#routePoints').value='30,70\\n31,71';return redrawRouteDraft();}")
                    check('Нельзя получить температурный график из RGB','()=>document.querySelector("#calculateRoute").disabled&&document.querySelector("#routeHint").textContent.includes("не являются температурой")')
                    # Длинный список файлов не должен уводить заголовок за экран.
                    extra=[]
                    for i in range(35):
                        extra.append(dict(assets[0],id=f'meta-{i}',category='metadata',channel=0,filename=f'SYNTHETIC-metadata-{i}.json',uri='https://s3.gptl.ru/SYNTHETIC-TEST/metadata.json'))
                    app.store.upsert([],extra)
                    page.locator('.catalog-files').click();wait('()=>S.fileAssets.length===37')
                    close=page.locator('#filesDialog [data-close]');before=close.bounding_box()
                    page.locator('#filesDialog .dialog-body').evaluate('(e)=>e.scrollTop=e.scrollHeight')
                    check('Список файлов действительно прокручивается', '()=>document.querySelector("#filesDialog .dialog-body").scrollTop>100')
                    after=close.bounding_box();assert abs(after['y']-before['y'])<1
                    check('Закрытие поверх прокрутки списка файлов','()=>{const e=document.querySelector("#filesDialog [data-close]"),r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}')
                    page.screenshot(path=str(out/'files-scrolled.png'));close.click()
                    # Новые отображаются первыми; тест не меняет FIFO работающей очереди.
                    app.queue.close()
                    for i,a in enumerate(extra):
                        app.store.enqueue(a,root/a['filename']);app.store.update_job(a['id'],state='done' if i<33 else 'queued')
                    app.store.update_job(extra[-2]['id'],state='running')
                    open_queue()
                    check('Выполняемая загрузка сверху','()=>document.querySelector("#queueRows [data-job]").dataset.job==="meta-33"')
                    check('Новая ожидающая перед историей','()=>document.querySelectorAll("#queueRows [data-job]")[1].dataset.job==="meta-34"')
                    close=page.locator('#queueDialog [data-close]');before=close.bounding_box()
                    page.locator('#queueDialog .dialog-body').evaluate('(e)=>e.scrollTop=e.scrollHeight')
                    check('Очередь действительно прокручивается','()=>document.querySelector("#queueDialog .dialog-body").scrollTop>100')
                    assert abs(close.bounding_box()['y']-before['y'])<1
                    check('Закрытие очереди не перекрывается содержимым','()=>{const e=document.querySelector("#queueDialog [data-close]"),r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}')
                    page.screenshot(path=str(out/'queue-scrolled.png'))
                    page.keyboard.press('Escape');check('Escape закрывает верхнее окно','()=>!document.querySelector("#queueDialog").open')
                    open_queue();page.locator('#queueDialog .dialog-body').evaluate('(e)=>e.scrollTop=0');page.screenshot(path=str(out/'queue-current.png'));page.keyboard.press('Escape')
                    # Возврат к числовым каналам после готового изображения.
                    page.evaluate("()=>setDate('2026-01-01')")
                    check('Обычные поканальные сцены не сломаны','()=>S.product?.product==="channel"&&!S.busy&&!UI.activeBuild')
                    check('Калибровка снова доступна на канале','()=>!document.querySelector("#calibrationOpen").disabled')
                    for width in (1024,390):
                        page.set_viewport_size(dict(width=width,height=844));open_queue()
                        page.locator('#queueDialog .dialog-body').evaluate('(e)=>e.scrollTop=e.scrollHeight')
                        check('Тело окна прокручивается '+str(width),'()=>document.querySelector("#queueDialog .dialog-body").scrollTop>100')
                        check('Окно без переполнения '+str(width),'()=>document.documentElement.scrollWidth<=innerWidth+1')
                        check('Крестик доступен '+str(width),'()=>{const e=document.querySelector("#queueDialog [data-close]"),r=e.getBoundingClientRect();return r.y>=0&&r.bottom<=innerHeight&&e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}')
                        page.screenshot(path=str(out/f'queue-{width}.png'))
                        page.locator('#queueDialog .dialog-body').evaluate('(e)=>e.scrollTop=0')
                        page.screenshot(path=str(out/f'queue-top-{width}.png'));page.keyboard.press('Escape')
                    assert not report['browser_errors'],report['browser_errors']
                except Exception:
                    page.screenshot(path=str(out/'failure.png'));raise
                finally:browser.close()
        except Exception as exc:report['error']=str(exc).replace(server.key,'[SESSION]');raise
        finally:
            (out/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            server.shutdown();thread.join(5);server.server_close();app.close()
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
