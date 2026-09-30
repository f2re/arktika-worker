#!/usr/bin/env python3
"""Native HTTP/Chromium checks. All weather fields are SYNTHETIC software fixtures."""
from __future__ import annotations
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
from pyproj import Transformer
from server import LocalServer
from browser_support import mount
from arktika.workstation import Workstation
from test_reanalysis import dataset


def main():
    p=argparse.ArgumentParser();p.add_argument('--executable');p.add_argument('--bridge',action='store_true');p.add_argument('--output',default='browser-results/reanalysis');a=p.parse_args()
    out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=True)
    report={'mode':'explicit-http-bridge; browser Origin/cookies/CSP not verified' if a.bridge else 'native-http','fixture':'SYNTHETIC SOFTWARE TESTS, NOT WEATHER OBSERVATIONS','checks':[],'browser_errors':[],'network':'localhost only; no live CDS/Earthdata test'}
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);app=Workstation(root/'state');app.store.set_setting('last_date','2024-01-01')
        app.store.set_setting('view_settings',{'preset':'barents','product':'channel','channel':9})
        paths={}
        for source,offset in [('era5',0),('merra2',2)]:
            path=root/('SYNTHETIC-'+source+'.nc');dataset(source,offset=offset).to_netcdf(path,engine='scipy');paths[source]=path
        server=LocalServer(('127.0.0.1',0),app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_address[1])
        try:
            with sync_playwright() as pw:
                launch={'headless':True}
                if a.executable:launch['executable_path']=a.executable
                browser=pw.chromium.launch(**launch);context=browser.new_context(viewport={'width':1536,'height':960})
                page=context.new_page();page.on('pageerror',lambda e:report['browser_errors'].append(str(e)))
                def wait(expression,timeout=30000):
                    end=time.monotonic()+timeout/1000
                    while time.monotonic()<end:
                        if page.evaluate(expression):return
                        page.wait_for_timeout(100)
                    raise AssertionError('Condition not reached: '+expression+'; errors='+str(report['browser_errors'])+'; status='+str(page.locator('#fieldError').text_content()))
                def check(name,expression):
                    wait(expression);report['checks'].append({'name':name,'passed':True});print(name,flush=True)
                def screenshot(name):page.screenshot(path=str(out/name))
                def import_field(source,count):
                    page.locator('#fieldSource').select_option(source)
                    page.locator('#fieldVariable').select_option('t');page.locator('#fieldLevel').select_option('850')
                    page.locator('#fieldTime').fill('2024-01-01T00:00')
                    page.locator('#fieldPath').evaluate('(e)=>e.closest("details").open=true')
                    page.locator('#fieldPath').fill(str(paths[source]));page.locator('#fieldImport').click()
                    check('Локальное поле '+source+' прошло настоящий обработчик',f'()=>FIELDS.stack.length==={count}&&FIELDS.job.status==="done"')
                    page.locator('#fieldClose').click()
                    check('Растровое наложение '+source+' показано',f'()=>document.querySelectorAll("#reanalysisLayers>g").length==={count}')
                mount(page,ROOT,server,a.bridge)
                check('Запуск без спутниковых файлов', '()=>typeof FIELDS!=="undefined"&&FIELDS.catalogue.length===3&&S.map!==null&&S.scene===null')
                page.locator('#authOpen').click();page.locator('#sourcesDialog [data-field-source=era5]').click()
                check('Источник ведёт непосредственно к полям', '()=>document.querySelector("#fieldDialog").open&&!document.querySelector("#calibrationDialog").open')
                page.locator('#fieldDownload').click()
                check('Без доступа нет скрытого сетевого запроса', '()=>document.querySelector("#fieldError").textContent.includes("Настройте доступ")')
                screenshot('01-field-dialog.png')
                import_field('era5',1)
                page.locator('#productRail').click()
                check('Слой содержит срок и единицы','()=>document.querySelector("#fieldLayerRows").textContent.includes("2024-01-01")&&document.querySelector(".field-legend").textContent.includes("°C")')
                identity=page.evaluate('()=>FIELDS.stack[0].id')
                page.locator(f'[data-field-style="{identity}"]').select_option('fill_contours')
                check('Изолинии поверх заливки','()=>document.querySelectorAll("#reanalysisLayers .field-contour").length>0')
                page.locator(f'[data-field-opacity="{identity}"]').fill('0.35')
                page.locator(f'[data-field-opacity="{identity}"]').dispatch_event('change')
                check('Прозрачность меняется без загрузки поля','()=>document.querySelector("#reanalysisLayers>g").getAttribute("opacity")==="0.35"')
                page.locator(f'[data-field-visible="{identity}"]').uncheck()
                check('Скрытый слой снят с карты','()=>document.querySelectorAll("#reanalysisLayers>g").length===0')
                page.locator(f'[data-field-visible="{identity}"]').check()
                check('Скрытый слой возвращается из кэша','()=>document.querySelectorAll("#reanalysisLayers>g").length===1')
                # Mouse click, transformed from known model coordinates into the native map viewport.
                g=page.evaluate('()=>S.map.grid');xx,yy=Transformer.from_crs(4326,g['crs'],always_xy=True).transform(30,70)
                bounds=g['bounds'];x=(xx-bounds[0])/(bounds[2]-bounds[0])*g['width'];y=(bounds[3]-yy)/(bounds[3]-bounds[1])*g['height']
                xy=page.evaluate('(p)=>{const q=document.querySelector("#map").createSVGPoint();q.x=p[0];q.y=p[1];const c=q.matrixTransform(document.querySelector("#map").getScreenCTM());return [c.x,c.y]}',[x,y])
                page.mouse.click(*xy)
                check('Анализ точки работает без снимка','()=>document.querySelector("#fieldProbe")?.textContent.includes("ERA5")&&S.product===null')
                screenshot('02-model-map-probe.png')
                page.locator('#closeInspector').click();page.locator('#fieldAddOpen').click()
                import_field('merra2',2)
                page.locator('#fieldAddOpen').click()
                page.locator('#fieldSource').select_option('era5');page.locator('#fieldConfigure').click()
                check('Настройка доступа из окна поля','()=>document.querySelector("#cdsAccessDialog").open')
                page.locator('#cdsToken').fill('SYNTHETIC-BROWSER-TOKEN-NOT-A-CREDENTIAL');page.locator('#cdsSave').click()
                check('Возврат к полю после настройки CDS','()=>document.querySelector("#fieldDialog").open&&document.querySelector("#fieldSource").value==="era5"')
                page.locator('#fieldDifferenceDetails').evaluate('(e)=>e.open=true')
                second=page.evaluate('()=>FIELDS.fields.find(f=>f.source==="merra2").id')
                page.locator('#fieldFirst').select_option(second);page.locator('#fieldSecond').select_option(identity)
                page.locator('#fieldDifference').click()
                check('Разность создана на сервере','()=>FIELDS.stack.length===3&&FIELDS.fields.some(f=>f.source==="difference")')
                page.locator('#fieldClose').click()
                check('Три независимых слоя одновременно','()=>document.querySelectorAll("#reanalysisLayers>g").length===3')
                screenshot('03-difference-stack.png')
                if a.bridge:
                    page.close();page=context.new_page();page.on('pageerror',lambda e:report['browser_errors'].append(str(e)));mount(page,ROOT,server,True)
                else: page.reload()
                check('Стек восстанавливается после перезагрузки страницы','()=>typeof FIELDS!=="undefined"&&FIELDS.stack.length===3&&document.querySelectorAll("#reanalysisLayers>g").length===3')
                page.locator('#authOpen').click();page.locator('#sourcesDialog [data-field-source=carra2]').click()
                page.locator('#fieldTime').fill('2024-01-01T01:00');page.locator('#fieldDownload').click()
                check('Промежуточный прогноз CARRA2 не подменяет анализ','()=>document.querySelector("#fieldError").textContent.includes("шагом 3")')
                for width,height in ((1536,960),(1024,768),(390,844)):
                    page.set_viewport_size({'width':width,'height':height})
                    check(f'Закрытие окна доступно при {width}×{height}', '()=>{const b=document.querySelector("#fieldClose").getBoundingClientRect();return b.top>=0&&b.right<=innerWidth&&b.bottom<=innerHeight;}')
                    screenshot(f'04-dialog-{width}.png')
                page.locator('#fieldClose').click()
                self_errors=report['browser_errors'];assert not self_errors,self_errors
                report['checks'].append({'name':'Ошибок JavaScript не было','passed':True})
                browser.close()
        finally:
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            app.cancel.set()
            if app.task and app.task.is_alive():app.task.join(5)
            server.shutdown();server.server_close();app.close()
    print('Passed',len(report['checks']),'browser checks',flush=True)

if __name__=='__main__':main()
