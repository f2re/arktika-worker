#!/usr/bin/env python3
"""Product-first flow on synthetic data; real HTTP, cookies, CSP, and download queue."""
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
from arktika.model import normalize_item
from arktika.workstation import Workstation
from server import LocalServer
from test_science import fixture
from test_archive import RGBTransport


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='browser-results/products');parser.add_argument('--executable');args=parser.parse_args()
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    report=dict(mode='native-http',fixture='SYNTHETIC SOFTWARE TEST; NOT WEATHER OBSERVATIONS',checks=[],browser_errors=[])
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);source=root/'source';source.mkdir();scene=fixture(source)
        calibrated=root/'calibrated';calibrated.mkdir();cal_scene=fixture(calibrated,units=True)
        transport=RGBTransport(Path(cal_scene['channels']['9']['path']).read_bytes())
        app=Workstation(root/'state',root/'downloads',client=AuthClient(transport=transport))
        app.register_file(scene['channels']['4']['path'])
        app.store.set_setting('last_date','2026-01-01');app.store.set_setting('view_settings',dict(preset='barents',product='channel',channel=4))
        remote=dict(type='Feature',id='SYNTHETIC-PRODUCT-FLOW',properties={'platform':'ARCM2','datetime':'2026-01-02T00:00:00Z','processing:level':'L2IR'},assets={})
        for ch in (7,9,10):remote['assets'][f'ch{ch:02d}']={'href':f'https://s3.gptl.ru/SYNTHETIC/A2_20260102000000_ch{ch:02d}.tif','type':'image/tiff','proj:epsg':4326}
        record,assets=normalize_item(remote);app.store.upsert([record],assets)
        server=LocalServer(('127.0.0.1',0),app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with sync_playwright() as pw:
                launch={'headless':True}
                if args.executable:launch['executable_path']=args.executable
                browser=pw.chromium.launch(**launch);page=browser.new_page(viewport=dict(width=1440,height=960))
                page.on('pageerror',lambda e:report['browser_errors'].append(str(e)))
                def wait(expression,timeout=45):
                    end=time.monotonic()+timeout
                    while time.monotonic()<end:
                        try:
                            if page.evaluate(expression):return
                        except Exception:pass
                        page.wait_for_timeout(100)
                    raise AssertionError(expression)
                def check(name,expr):wait(expr);report['checks'].append(dict(name=name,passed=True))
                def shot(name):page.screenshot(path=str(out/name))
                try:
                    page.goto(base+'/#'+server.key)
                    check('Начальный одиночный канал','()=>typeof S!=="undefined"&&S.product?.request.channel===4&&!S.busy&&!UI.activeBuild')
                    for ch in (5,6,7,8,9,10):app.register_file(scene['channels'][str(ch)]['path'])
                    check('Активный срок получает докачанные каналы без перевыбора','()=>S.scene.channels.length===7')
                    page.locator('#catalogTask').select_option('all')
                    page.locator('#productRail').click()
                    check('Продукты и каналы остаются выбираемыми','()=>[...document.querySelectorAll("#product option,#channel option,#taskGrid button")].every(e=>!e.disabled)')
                    check('Неполный набор не блокирует уже доступное','()=>document.querySelector("#sessions button.session").textContent==="Открыть доступное"')
                    page.locator('#product').select_option('micro24')
                    check('Неизвестные единицы дают действие, а не немой запрет','()=>document.querySelector("#productFeedback").textContent.includes("Настроить шкалу")&&!document.querySelector("#calibrationDialog").open')
                    page.locator('#productFeedback button').click()
                    check('Для продукта отмечены только нужные каналы','()=>[...document.querySelectorAll("#scaleChannels input:checked")].map(e=>Number(e.value)).join(",")==="7,9,10"')
                    check('Нет обязательного JSON-редактора','()=>!document.querySelector("#calJson")')
                    page.locator('#calMode').select_option('assumed')
                    wait('()=>document.querySelector("#scalePreview").textContent.includes("Tя =")')
                    page.locator('#scaleImageButton').click()
                    check('Предпросмотр продукта без сохранения','()=>document.querySelector("#scaleImageResult img")?.complete&&document.querySelector("#scaleImageResult").textContent.includes("не изменены")')
                    assert app.store.setting('scale_profiles',{})=={}
                    page.locator('#saveCalibration').click();page.locator('#confirmYes').click()
                    check('Шкала сохраняется и продукт строится сам','()=>S.product?.product==="micro24"&&S.product.calibration_status==="assumed"&&!S.busy&&!UI.activeBuild')
                    shot('products-ready.png')
                    page.locator('#product').select_option('phase')
                    check('Повторного запроса шкалы нет','()=>S.product?.product==="phase"&&!S.busy&&!UI.activeBuild&&!document.querySelector("#calibrationDialog").open')
                    page.locator('#calibrationOpen').click();page.locator('#calMode').select_option('anchors')
                    for sel,val in (('#scaleDN1','100'),('#scaleT1','220'),('#scaleDN2','500'),('#scaleT2','300'),('#scaleReference','SYNTHETIC arithmetic example; not observations')):page.locator(sel).fill(val)
                    check('Две опоры пересчитывают формулу','()=>document.querySelector("#scalePreview").textContent.includes("0,2")&&document.querySelector("#scalePreview").textContent.includes("200")')
                    shot('scale-two-points.png')
                    page.locator('#scaleDN2').fill('100')
                    check('Вырожденные опоры не принимаются','()=>!document.querySelector("#scaleError").hidden&&document.querySelector("#scalePreview").childElementCount===0')
                    page.locator('#calibrationDialog [data-close]').click()
                    page.locator('[data-task="cth"]').click()
                    check('Высота открывает канал и профильный сценарий','()=>S.product?.product==="channel"&&S.product.request.channel===9&&!S.busy&&!UI.activeBuild&&document.querySelector("#productFeedback").textContent.includes("профиль T(z)")')
                    page.evaluate('()=>setDate("2026-01-02")')
                    wait('()=>CATALOG.rows.length===1&&S.scene===null')
                    page.evaluate('()=>left("product")');page.locator('#product').select_option('micro24')
                    check('Без срока открывается его выбор с сохранённой задачей','()=>PRODUCT_FLOW.awaitingTime?.product==="micro24"&&!document.querySelector("#leftPanel").hidden')
                    page.locator('.catalog-card header').click()
                    check('Продукт сам докачивает ровно три нужных канала','()=>S.product?.product==="micro24"&&S.product.time==="2026-01-02T00:00:00Z"&&!S.busy&&!UI.activeBuild')
                    assert len(app.store.jobs())==3 and transport.calls>0
                    check('Метаданные K используются без ручной настройки','()=>S.product.calibration_status==="metadata"&&!document.querySelector("#calibrationDialog").open')
                    page.locator('#productQuick').click();shot('metadata-ready.png')
                    for width in (1024,390):
                        page.set_viewport_size(dict(width=width,height=844));page.locator('#calibrationOpen').click()
                        page.locator('#calibrationDialog').wait_for(state='visible')
                        check('Шкала без горизонтального переполнения '+str(width),'()=>document.documentElement.scrollWidth<=innerWidth+1&&document.querySelector("#calibrationDialog").scrollWidth<=document.querySelector("#calibrationDialog").clientWidth+1')
                        shot('scale-'+str(width)+'.png');page.keyboard.press('Escape')
                    doc=page.context.new_page();resp=doc.goto(base+'/docs/CALIBRATION.html')
                    assert resp.status==200 and 'text/html' in resp.headers.get('content-type','');doc.close()
                    report['checks'].append(dict(name='HTML-методика доступна',passed=True))
                    assert not report['browser_errors'],report['browser_errors']
                except Exception:
                    try:
                        report['flow_state']=page.evaluate('()=>({scene:S.scene?.id,product:S.product?.product,selected:document.querySelector("#product").value,pending:PRODUCT_FLOW.pending,active:UI.activeBuild,busy:S.busy,feedback:document.querySelector("#productFeedback")?.textContent,scale_error:document.querySelector("#scaleError")?.textContent})')
                    except Exception:
                        pass
                    shot('failure.png');raise
                finally:browser.close()
        except Exception as exc:report['error']=str(exc).replace(server.key,'[SESSION]');raise
        finally:
            (out/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            server.shutdown();thread.join(5);server.server_close();app.close()
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
