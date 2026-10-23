#!/usr/bin/env python3
"""Native HTTP/Chromium checks. All weather fields are SYNTHETIC software fixtures."""
from __future__ import annotations
import argparse
import json
import os
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
from test_adapter_credentials import adapter as custom_test_adapter
from arktika.reanalysis.adapters import register_adapter, unregister_adapter


def main():
    p=argparse.ArgumentParser();p.add_argument('--executable');p.add_argument('--bridge',action='store_true');p.add_argument('--output',default='browser-results/reanalysis');a=p.parse_args()
    out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=True)
    report={'mode':'explicit-http-bridge; browser Origin/cookies/CSP not verified' if a.bridge else 'native-http','fixture':'SYNTHETIC SOFTWARE TESTS, NOT WEATHER OBSERVATIONS','checks':[],'browser_errors':[],'network':'localhost only; no live CDS/Earthdata test'}
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);isolated_home=root/'home';isolated_home.mkdir();os.environ['HOME']=str(isolated_home)
        app=Workstation(root/'state');app.store.set_setting('last_date','2024-01-01')
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
                browser_console=[]
                page=context.new_page();page.on('pageerror',lambda e:report['browser_errors'].append(str(e)))
                page.on('console',lambda message:browser_console.append(message.text))
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
                check('Без доступа открывается форма CDS и сохраняется исходный запрос без запуска задания', '()=>document.querySelector("#cdsAccessDialog").open&&FIELDS.credentialRetry?.source==="era5"&&!FIELDS.pending')
                screenshot('01-field-dialog.png')
                page.locator('#cdsAccessDialog [data-close]').click()
                check('Отмена доступа возвращает сохранённый запрос к настройке', '()=>document.querySelector("#fieldDialog").open&&!FIELDS.credentialRetry&&FIELDS.job.status==="needs_action"&&!document.querySelector("#fieldDialogCancel").hidden')
                page.locator('#fieldDialogCancel').click()
                check('Сохранённый сетевой запрос можно явно отменить перед локальным импортом', '()=>FIELDS.job.status==="cancelled"&&!FIELDS.job.resume_available')
                # A trusted third-party plugin declares its own credential schema.
                # Stop the resumed worker deliberately after the request crosses the
                # credential boundary; no real provider/network is contacted.
                custom_adapter=custom_test_adapter()
                object.__setattr__(custom_adapter,'planner',lambda request:{**request,'resolution_km':10,'time_kind':'analysis','key':'synthetic-plan-key'})
                register_adapter(custom_adapter)
                custom_source='custom_reanalysis'
                page.evaluate('''()=>{const s=document.querySelector('#fieldSource');const o=document.createElement('option');o.value='custom_reanalysis';o.textContent='Тестовый источник';s.append(o);return openFieldDialog('custom_reanalysis')}''')
                page.locator('#fieldSource').select_option(custom_source)
                page.locator('#fieldVariable').select_option('mslp')
                page.locator('#fieldTime').fill('2024-01-01T00:00')
                custom_payloads=[]
                real_field_process=app._field_process
                def stop_after_custom_resume(payload,**kwargs):
                    custom_payloads.append(payload)
                    raise ValueError('Синтетическая остановка браузерного теста.')
                app._field_process=stop_after_custom_resume
                page.locator('#fieldDownload').click()
                check('Сторонний адаптер приостанавливает точный запрос до ввода реквизитов',
                      '()=>document.querySelector("dialog[data-credential-provider=research_auth]")?.open&&document.querySelector("dialog[data-credential-provider=research_auth]").querySelectorAll("input").length===2&&FIELDS.job.status==="needs_action"')
                credential_dialog=page.locator('dialog[data-credential-provider="research_auth"]')
                check('Поля формы строятся по безопасной схеме адаптера',
                      '()=>{const d=document.querySelector("dialog[data-credential-provider=research_auth]");return d&&d.textContent.includes("Доступ тестового источника.")&&d.querySelectorAll("input[type=text]").length===1&&d.querySelectorAll("input[type=password]").length===1&&d.textContent.includes("Имя пользователя")&&d.textContent.includes("Токен")}')
                custom_task_id=page.evaluate('()=>FIELDS.credentialRetry.id')
                credential_dialog.locator('input[type="text"]').fill('synthetic-operator')
                credential_dialog.locator('input[type="password"]').fill('SYNTHETIC-ADAPTER-SECRET')
                credential_dialog.get_by_role('button',name='Сохранить в памяти и продолжить').click()
                check('После ввода реквизитов продолжается тот же запрос',
                      '()=>FIELDS.job.id==="'+custom_task_id+'"&&FIELDS.job.status==="error"&&FIELDS.credentialRetry===null&&document.querySelector("dialog[data-credential-provider=research_auth]")===null')
                assert len(custom_payloads)==1, 'custom provider request was not resumed exactly once'
                assert 'SYNTHETIC-ADAPTER-SECRET' in json.dumps(custom_payloads[0])
                assert custom_payloads[0]['plan']['source']==custom_source
                assert custom_payloads[0]['plan']['time']=='2024-01-01T00:00:00Z'
                public_state=json.dumps({'job':app._field_job,'source_state':app.source_state(),
                                         'provider_status':app.provider_credential_status('research_auth')},ensure_ascii=False)
                browser_state=page.evaluate('''()=>JSON.stringify({fields:FIELDS,local:localStorage,session:sessionStorage,
                    text:document.body.innerText,values:[...document.querySelectorAll('input')].map(x=>x.value),
                    attributes:[...document.querySelectorAll('*')].flatMap(x=>[...x.attributes].map(a=>a.value))})''')
                assert 'SYNTHETIC-ADAPTER-SECRET' not in public_state
                assert 'SYNTHETIC-ADAPTER-SECRET' not in browser_state
                assert 'SYNTHETIC-ADAPTER-SECRET' not in '\n'.join(browser_console)
                report['checks'].append({'name':'Реквизиты не попали в UI state, storage, DOM или публичный state сервера','passed':True});print(report['checks'][-1]['name'],flush=True)
                app.clear_provider_credential('research_auth')
                app._field_process=real_field_process
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
                from arktika.reanalysis_service import RetryableFieldError
                cached_meta=app.field(identity)
                def fail_remote(*args,**kwargs):raise RetryableFieldError('Временная ошибка сети. Повторите сохранённый запрос.')
                app._field_process=fail_remote
                page.locator('#fieldTime').fill('2024-01-01T00:00');page.locator('#fieldDownload').click()
                check('Временный сбой показывает повтор сохранённого задания','()=>FIELDS.job.status==="error"&&FIELDS.job.retry_available===true&&!document.querySelector("#fieldRetry").hidden')
                retry_id=page.evaluate('()=>FIELDS.job.id')
                app._field_process=lambda payload,**kwargs:{'field':cached_meta,'cached':True}
                page.locator('#fieldDialogRetry').click()
                check('Повтор использует тот же id задания и завершает запрос','()=>FIELDS.job.status==="done"&&FIELDS.job.id==="'+retry_id+'"')
                # Keep the deterministic cached response only for the retry itself;
                # difference must exercise the real worker and produce a derived field.
                app._field_process=real_field_process
                page.locator('#fieldDifferenceDetails').evaluate('(e)=>e.open=true')
                second=page.evaluate('()=>FIELDS.fields.find(f=>f.source==="merra2").id')
                page.locator('#fieldFirst').select_option(second);page.locator('#fieldSecond').select_option(identity)
                page.locator('#fieldDifference').click()
                check('Разность создана на сервере','()=>FIELDS.job.status==="done"&&FIELDS.stack.length===3&&FIELDS.fields.some(f=>f.source==="difference")')
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
        unregister_adapter('custom_reanalysis')
    print('Passed',len(report['checks']),'browser checks',flush=True)

if __name__=='__main__':main()
