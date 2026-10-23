#!/usr/bin/env python3
"""Product-first flow on synthetic data; real HTTP, cookies, CSP, and download queue."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from playwright.sync_api import sync_playwright
from arktika.auth import AuthClient
from arktika.model import normalize_item
from arktika.workstation import Workstation
from server import LocalServer
from test_science import fixture
from arktika.processing import build_product
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
        # Stable synthetic numeric products let the browser prove product-scoped styling.
        style_first=build_product(cal_scene,app.product_root,dict(product='channel',preset='barents',width=256,channel=9),{'mode':'unknown'})
        style_second=build_product(cal_scene,app.product_root,dict(product='difference',preset='barents',width=256,channel=9),{'mode':'unknown'})
        style_baseline={}
        for product in (style_first,style_second):
            values_path=app.artifact(product['id'],'values.tif')
            style_baseline[product['id']]={'values_sha256':hashlib.sha256(values_path.read_bytes()).hexdigest(),
                'passport':json.loads(json.dumps(product['passport']))}
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
                browser=pw.chromium.launch(**launch);context=browser.new_context(viewport=dict(width=1440,height=960));page=context.new_page()
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
                def open_saved_product(identity):
                    page.locator('#productsOpen').click()
                    page.locator('#productsDialog').wait_for(state='visible')
                    page.locator(f'#savedProducts button[data-id="{identity}"]').click()
                    wait(f'()=>S.product?.id==={json.dumps(identity)}&&!S.busy&&!UI.activeBuild')
                    style_state=page.evaluate('()=>({id:S.product?.id,files:S.product?.files,quantity_kind:S.product?.legend?.quantity_kind,version:S.product?.passport?.algorithm?.version,button:!!document.querySelector("#productStyleOpen"),disabled:document.querySelector("#productStyleOpen")?.disabled})')
                    assert style_state['button'] and not style_state['disabled'], f'Продукт открылся, но шкала недоступна: {style_state}'
                def export_product_json(identity):
                    encoded=page.evaluate('''async id=>{const r=await fetch('/artifact/'+encodeURIComponent(id)+'/export.zip');if(!r.ok)throw Error('ZIP HTTP '+r.status);const bytes=new Uint8Array(await r.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));return btoa(binary)}''',identity)
                    with zipfile.ZipFile(io.BytesIO(base64.b64decode(encoded))) as bundle:
                        names=set(bundle.namelist())
                        assert {'style.json','legend.json','product.json'}<=names,names
                        return {name:json.loads(bundle.read(name)) for name in ('style.json','legend.json','product.json')}
                def save_browser_style(identity,minimum,maximum,palette_id,name,default_minimum,default_maximum):
                    open_saved_product(identity)
                    page.locator('#productStyleOpen').click()
                    page.locator('#productStyleDialog').wait_for(state='visible')
                    check('Профиль стиля привязан к продукту и версии '+name,
                          '()=>document.querySelector("#productStyleIdentity").textContent.includes('+json.dumps(identity)+')&&document.querySelector("#productStyleIdentity").textContent.includes(S.product.passport.algorithm.version)')
                    profile=app.product_style({'id':identity})['style']
                    absolute_temperature=app.product(identity)['legend']['quantity_kind']=='absolute_temperature' and app.product(identity)['legend']['units']=='K'
                    expected_minimum=profile['minimum']-273.15 if absolute_temperature else profile['minimum']
                    expected_maximum=profile['maximum']-273.15 if absolute_temperature else profile['maximum']
                    assert float(page.locator('#productStyleMinimum').input_value())==float(default_minimum if default_minimum is not None else expected_minimum)
                    assert float(page.locator('#productStyleMaximum').input_value())==float(default_maximum if default_maximum is not None else expected_maximum)
                    if absolute_temperature:
                        check('Пределы абсолютной температуры показаны в °C как в легенде','()=>document.querySelector("#productStyleUnits").textContent==="°C"')
                    if name=='first':
                        page.set_viewport_size(dict(width=390,height=844))
                        check('Панель шкалы работает на узком окне','()=>document.documentElement.scrollWidth<=innerWidth+1&&document.querySelector("#productStyleDialog").scrollWidth<=document.querySelector("#productStyleDialog").clientWidth+1')
                        page.set_viewport_size(dict(width=1440,height=960))
                        page.locator('#productStyleMinimum').fill(str(maximum))
                        page.locator('#productStyleMaximum').fill(str(maximum))
                        page.locator('#productStyleSave').click()
                        check('Ошибочный диапазон не сохраняется','()=>document.querySelector("#productStyleDialog").open&&document.querySelector("#productStyleStatus").textContent.includes("меньше максимума")')
                    page.locator('#productStylePalette').select_option(palette_id)
                    page.locator('#productStyleMinimum').fill(str(minimum));page.locator('#productStyleMaximum').fill(str(maximum))
                    page.locator('#productStyleSave').click()
                    stored_minimum=minimum+273.15 if absolute_temperature else minimum
                    stored_maximum=maximum+273.15 if absolute_temperature else maximum
                    wait('()=>!document.querySelector("#productStyleDialog").open&&S.product?.id==='+json.dumps(identity)+'&&Math.abs(S.product.legend.display_min-'+str(stored_minimum)+')<1e-9&&Math.abs(S.product.legend.display_max-'+str(stored_maximum)+')<1e-9')
                    check('Карта и легенда обновляются после сохранения '+name,
                          '()=>document.querySelector("#raster").getAttribute("href").includes("?style=")&&document.querySelector("#legend .colorbar").style.backgroundImage.includes("linear-gradient")&&document.querySelector("#legend .scale-labels").textContent.includes('+json.dumps(str(minimum))+')&&document.querySelector("#legend .scale-labels").textContent.includes('+json.dumps(str(maximum))+')')
                    report['checks'].append(dict(name='Сохранение шкалы с явным действием '+name,passed=True))
                    assert hashlib.sha256(app.artifact(identity,'values.tif').read_bytes()).hexdigest()==style_baseline[identity]['values_sha256']
                    assert app.product(identity)['passport']==style_baseline[identity]['passport']
                    bundle=export_product_json(identity)
                    assert bundle['style.json']['product_id']==identity
                    assert bundle['style.json']['algorithm_version']==style_baseline[identity]['passport']['algorithm']['version']
                    assert bundle['style.json']['minimum']==stored_minimum and bundle['style.json']['maximum']==stored_maximum and bundle['style.json']['palette_id']==palette_id
                    assert bundle['legend.json']['display_min']==stored_minimum and bundle['legend.json']['display_max']==stored_maximum
                    assert bundle['product.json']['passport']==style_baseline[identity]['passport']
                    report['checks'].append(dict(name='values.tif и паспорт неизменны; экспорт содержит актуальные style.json и legend.json '+name,passed=True))
                    shot('product-style-'+name+'.png')
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
                    page.locator('#calibrationOpen').click()
                    check('Настройка из продукта выбирает его каналы, а не старый канал 4','()=>[...document.querySelectorAll("#scaleChannels input:checked")].map(e=>Number(e.value)).join(",")=="7,9,10"')
                    page.locator('#calMode').select_option('anchors')
                    for sel,val in (('#scaleDN1','100'),('#scaleT1','220'),('#scaleDN2','500'),('#scaleT2','300'),('#scaleReference','SYNTHETIC arithmetic example; not observations')):page.locator(sel).fill(val)
                    check('Две опоры пересчитывают формулу','()=>document.querySelector("#scalePreview").textContent.includes("0,2")&&document.querySelector("#scalePreview").textContent.includes("200")')
                    shot('scale-two-points.png')
                    page.locator('#scalePreview').scroll_into_view_if_needed();shot('scale-preview.png')
                    page.locator('#scaleDN2').fill('100')
                    check('Вырожденные опоры не принимаются','()=>!document.querySelector("#scaleError").hidden&&document.querySelector("#scalePreview").childElementCount===0')
                    page.locator('#calibrationDialog [data-close]').click()
                    page.locator('[data-task="cth"]').click()
                    check('Высота открывает канал и профильный сценарий','()=>S.product?.product==="channel"&&S.product.request.channel===9&&!S.busy&&!UI.activeBuild&&document.querySelector("#productFeedback").textContent.includes("профиль T(z)")')
                    page.evaluate('()=>setDate("2026-01-02")')
                    wait('()=>CATALOG.rows.length===1&&S.scene===null')
                    page.evaluate('()=>left("product")');page.locator('#product').select_option('micro24')
                    check('Без срока открывается его выбор с сохранённой задачей','()=>PRODUCT_FLOW.awaitingTime?.product==="micro24"&&!document.querySelector("#leftPanel").hidden')
                    page.locator('.catalog-card button.session').click()
                    check('Продукт сам докачивает ровно три нужных канала','()=>S.product?.product==="micro24"&&S.product.time==="2026-01-02T00:00:00Z"&&!S.busy&&!UI.activeBuild')
                    assert len(app.store.jobs())==3 and transport.calls>0
                    check('Метаданные K используются без ручной настройки','()=>S.product.calibration_status==="metadata"&&!document.querySelector("#calibrationDialog").open')
                    page.locator('#productQuick').click();shot('metadata-ready.png')
                    check('Для готового RGB продукта экранная шкала недоступна','()=>S.product?.product==="micro24"&&document.querySelector("#productStyleOpen").disabled')
                    save_browser_style(style_first['id'],-3,7,'blue-amber','first',None,None)
                    first_saved=export_product_json(style_first['id'])['style.json']
                    save_browser_style(style_second['id'],0,2,'multicolor','second',-4,2)
                    second_saved=export_product_json(style_second['id'])['style.json']
                    assert style_first['id']!=style_second['id']
                    assert first_saved['product_id']==style_first['id'] and second_saved['product_id']==style_second['id']
                    open_saved_product(style_first['id'])
                    check('Возврат к первому продукту восстанавливает его шкалу и легенду',
                          '()=>S.product?.id==='+json.dumps(style_first['id'])+'&&Math.abs(S.product.legend.display_min-270.15)<1e-9&&Math.abs(S.product.legend.display_max-280.15)<1e-9&&(document.querySelector("#legend .scale-labels").textContent.includes("−3")||document.querySelector("#legend .scale-labels").textContent.includes("-3"))&&document.querySelector("#legend .scale-labels").textContent.includes("7")')
                    restored=page.evaluate('id=>api("/api/product/style?id="+encodeURIComponent(id))',style_first['id'])
                    assert restored['style']['minimum']==270.15 and restored['style']['maximum']==280.15 and restored['style']['palette_id']=='blue-amber'
                    assert restored['style']['product_id']==style_first['id']
                    report['checks'].append(dict(name='Второй продукт не перезаписывает стиль первого; повторное открытие восстанавливает свой профиль',passed=True))
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
