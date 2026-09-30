#!/usr/bin/env python3
"""ERA5 user journey. Real app/server/queue; SYNTHETIC external data and engine.

Native localhost/cookies/CSP by default. --bridge tests rendering/actions only when
this workbench blocks browser localhost; CI always uses native HTTP.
"""
import argparse
from contextlib import ExitStack
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from playwright.sync_api import sync_playwright
from server import LocalServer
from arktika.download import digest
from arktika.era5_calculation import calculate_reference
from arktika.network import Cancelled
from browser_support import mount
from test_era5 import model_files
from test_era5_workflow import workflow_fixture,RUNTIME


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='browser-results/era5');parser.add_argument('--executable');parser.add_argument('--bridge',action='store_true');args=parser.parse_args()
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    report={'mode':'explicit-http-bridge' if args.bridge else 'native-http','fixture':'SYNTHETIC SOFTWARE TEST. External install/provider/RTTOV replaced; not meteorological validation.', 'checks':[],'browser_errors':[]}
    with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
        root=Path(tmp);app,scene,transport,assets=workflow_fixture(root)
        app.store.set_setting('last_date','2026-01-01');app.store.set_setting('view_settings',{'preset':'barents','product':'channel','channel':9})
        runtime=dict(RUNTIME,missing=['xarray','netCDF4','cdsapi']);control={'install_calls':0,'retrievals':0,'slow':False,'coefficient_calls':0,'coefficient_error':False}
        coefficient={'present':False,'name':'SYNTHETIC-coefficients.dat'}
        cache={}
        def ready(*args,**kwargs):return copy.deepcopy(runtime)
        def install(state,cancel,progress):
            control['install_calls']+=1
            progress('Устанавливаю библиотеки ERA5 — SYNTHETIC TEST INSTALLER.')
            for _ in range(7):
                if cancel.wait(.15):raise Cancelled()
            runtime['missing']=[];runtime['managed']=True;return ready()
        def download_table(state,cancel,progress):
            if coefficient['present']:return dict(coefficient)
            control['coefficient_calls']+=1
            progress('Коэффициенты: загрузка — SYNTHETIC TEST SOURCE')
            if cancel.wait(.3):raise Cancelled()
            if control['coefficient_error']:raise ValueError('Коэффициенты: SYNTHETIC TEST NETWORK ERROR')
            coefficient.update(present=True,sha256='0'*64,size_bytes=4096)
            return dict(coefficient)
        stack.enter_context(patch('arktika.era5_calculation.download_coefficients',side_effect=download_table))
        stack.enter_context(patch('arktika.auto_calibration.download_coefficients',side_effect=download_table))
        for module in ('arktika.auto_calibration','arktika.era5_workflow'):
            stack.enter_context(patch(module+'.coefficient_info',side_effect=lambda *a:dict(coefficient)))
        def calculate(scene,plan,options,state,credential,identity,runtime_,cancel,progress):
            if control['slow']:
                progress('ERA5: ожидаю тестовый источник — SYNTHETIC TEST')
                if cancel.wait(10):raise Cancelled()
            def fetch(plan,cred,folder,cancel,progress):
                key=json.dumps(plan['requests'],sort_keys=True)
                if key not in cache:
                    control['retrievals']+=1;folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
                    progress('ERA5: запрос — SYNTHETIC TEST SOURCE');time.sleep(.4)
                    cache[key]=model_files(folder,plan)
                else:progress('ERA5: файлы из проверенного тестового кэша')
                return cache[key]
            # Test radiances are explicitly synthetic. Actual regression code checks fits.
            x=np.linspace(180,310,80);profiles=[{'test_dn':float(v)} for v in x]
            pairs={'profiles':profiles,'dn':[[float(v)]*len(plan['channels']) for v in x],'groups':[str(i//10) for i in range(80)]}
            with patch('arktika.era5_calculation.retrieve_plan',side_effect=fetch),patch('arktika.era5_calculation.collocate',return_value=pairs),patch('arktika.era5_calculation.forward_isolated',side_effect=lambda profiles,channels,*a:np.array([[.5*p['test_dn']+130]*len(channels) for p in profiles])):
                return calculate_reference(scene,plan,options,state,credential,identity,cancel,progress)
        stack.enter_context(patch('arktika.era5_workflow.runtime_info',side_effect=ready))
        stack.enter_context(patch('arktika.auto_calibration.runtime_info',side_effect=ready))
        stack.enter_context(patch('arktika.era5_workflow.ensure_runtime',side_effect=install))
        stack.enter_context(patch('arktika.era5_workflow.run_calculation',side_effect=calculate))
        app.era5_credentials({'text':'SYNTHETIC-ONLY-ACCESS-TOKEN','format':'token'})
        server=LocalServer(('127.0.0.1',0),app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with sync_playwright() as pw:
                launch={'headless':True}
                if args.executable:launch['executable_path']=args.executable
                browser=pw.chromium.launch(**launch);context=browser.new_context(viewport={'width':1440,'height':960});page=context.new_page()
                page.on('pageerror',lambda e:report['browser_errors'].append(str(e)))
                def wait(expr,timeout=30):
                    until=time.monotonic()+timeout
                    while time.monotonic()<until:
                        try:
                            if page.evaluate(expr):return
                        except Exception:pass
                        page.wait_for_timeout(100)
                    raise AssertionError(expr)
                def check(name,expr):wait(expr);report['checks'].append({'name':name,'passed':True})
                def shot(name):page.screenshot(path=str(out/(name+'.png')))
                def show():
                    page.locator('#calibrationOpen').click();page.locator('#era5Open').click();wait('()=>ERA5_UI.preflight&&ERA5_UI.state&&!document.querySelector("#era5Run").disabled')
                try:
                    mount(page,ROOT,server,args.bridge)
                    wait('()=>typeof S!=="undefined"&&S.product&&!S.busy&&!UI.activeBuild')
                    # Prepare stale-channel situation directly; all interactions below use UI.
                    page.locator('#product').evaluate('(e)=>e.value="micro24"')
                    show()
                    check('Все нужные каналы, а не только локальный 9','()=>ERA5_UI.channels.join(",")==="7,9,10"&&ERA5_UI.preflight.inventory.ready.join(",")==="9"')
                    check('Установка библиотек предусмотрена действием запуска','()=>document.querySelector("#era5Steps").textContent.includes("Установятся")')
                    check('Основное действие впереди подробностей','()=>document.querySelector("#era5Run").getBoundingClientRect().y<document.querySelector("#era5Steps").getBoundingClientRect().y&&!document.querySelector("#era5Advanced").open')
                    shot('prepare-first')
                    page.locator('#era5Run').click()
                    check('Один запуск начал установку','()=>ERA5_UI.state?.job.steps?.some(s=>s.key==="dependencies"&&s.status==="running")')
                    page.locator('#era5Dialog [data-close]').click()
                    check('Закрытие не отменяет подготовку','()=>!document.querySelector("#era5Dialog").open')
                    time.sleep(.2);show()
                    check('Возврат показывает состояние той же задачи','()=>ERA5_UI.state.job.channels.join(",")==="7,9,10"')
                    wait('()=>ERA5_UI.state?.job.status==="data_ready"&&ERA5_UI.report')
                    check('Каналы докачаны и активный срок обновлён','()=>S.scene.channels.length===3&&ERA5_UI.state.job.steps.find(s=>s.key==="channels").status==="done"')
                    assert sorted(Path(j['path']).name[-6:-4] for j in app.store.jobs())==['07','10']
                    check('ERA5 готова даже без RTTOV','()=>ERA5_UI.state.job.next_action==="engine"&&document.querySelector("#era5Report").textContent.includes("Файлов ERA5 проверено")')
                    check('Шкала без RTTOV не объявлена готовой','()=>ERA5_UI.state.job.steps.find(s=>s.key==="calibration").status!=="done"&&!document.querySelector("#era5Apply")')
                    check('Есть следующий шаг без тупика','()=>document.querySelector("#era5Run").textContent.includes("Подключить RTTOV")&&!document.querySelector("#era5EngineHelp").hidden')
                    check('Коэффициенты получены даже без программы RTTOV','()=>ERA5_UI.state.runtime.coefficient.present&&ERA5_UI.state.job.steps.find(s=>s.key==="coefficients").status==="done"')
                    assert control['coefficient_calls']==1
                    check('Таблица и программа показаны отдельными этапами','()=>document.querySelector("[data-stage=coefficients]").textContent.includes("Электро-Л")&&document.querySelector("[data-stage=engine]").textContent.includes("Программа RTTOV")')
                    shot('data-ready')
                    # Resume the exact old paused state: ERA5 present, no table or engine.
                    page.locator('#era5Dialog [data-close]').click()
                    with app.lock:
                        app._era5_job['steps']=[v for v in app._era5_job['steps'] if v['key']!='coefficients']
                        app._save_flow()
                    coefficient['present']=False;control['coefficient_error']=True
                    counts=(control['retrievals'],control['install_calls'],transport.calls)
                    old_report=app._era5_job['report_id']
                    show()
                    check('Старое ожидание RTTOV само запускает докачку таблицы','()=>ERA5_UI.state.job.active_step==="coefficients"')
                    check('Сбой таблицы показывает отдельный повтор','()=>ERA5_UI.state.job.status==="error"&&document.querySelector("#era5Run").dataset.action==="coefficients"')
                    assert counts==(control['retrievals'],control['install_calls'],transport.calls)
                    shot('coefficient-error')
                    control['coefficient_error']=False
                    page.locator('#era5Run').click()
                    check('Повтор получает таблицу и возвращает ожидание программы','()=>ERA5_UI.state.job.status==="data_ready"&&ERA5_UI.state.runtime.coefficient.present&&document.querySelector("#era5Run").dataset.action==="engine"')
                    assert old_report==app._era5_job['report_id']
                    assert counts==(control['retrievals'],control['install_calls'],transport.calls)
                    report['checks'].append({'name':'Докачка таблицы сохранила отчёт и не повторила ERA5, каналы или установку','passed':True})
                    shot('coefficients-repaired')
                    calls=transport.calls
                    runtime['pyrttov']=True
                    page.evaluate('()=>era5Refresh()')
                    check('Угол не придуман автоматически','()=>!document.querySelector("#era5GeometryHelp").hidden&&document.querySelector("#era5Zenith").value===""')
                    page.locator('#era5Zenith').fill('23');page.locator('#era5Zenith').dispatch_event('change')
                    wait('()=>document.querySelector("#era5Run").dataset.action==="start"')
                    page.locator('#era5Run').click()
                    wait('()=>ERA5_UI.state.job.status==="ready"&&ERA5_UI.report?.status==="ready"')
                    check('Подбор действительно выполнен и три канала прошли проверку','()=>Object.values(ERA5_UI.report.channels).filter(c=>c.status==="passed").length===3')
                    assert calls==transport.calls;assert control['retrievals']==1
                    report['checks'].append({'name':'Продолжение не скачивает заново каналы и ERA5','passed':True})
                    shot('scale-ready')
                    page.locator('#era5Run').click()
                    check('Применение открывает запрошенный продукт','()=>!document.querySelector("#era5Dialog").open&&S.product?.product==="micro24"&&!S.busy&&!UI.activeBuild')
                    # Explicit data-only branch and access import auto-continuation.
                    show();page.locator('#era5Advanced').evaluate('(e)=>e.open=true');page.locator('#era5ClearCredential').click()
                    check('После удаления доступ можно ввести прямо в форме','()=>!document.querySelector("#era5Access").hidden&&!ERA5_UI.state.credentials.present')
                    page.locator('#era5CredentialFile').set_input_files({'name':'.cdsapirc','mimeType':'text/plain','buffer':b'url: https://cds.climate.copernicus.eu/api\nkey: SYNTHETIC-IMPORT-TOKEN'})
                    check('Импорт файла сам продолжает подготовку','()=>ERA5_UI.state.credentials.present&&ERA5_UI.state.job.status!=="applied"')
                    wait('()=>!ERA5_UI.state.busy')
                    check('Секрет не остаётся в DOM и public state','()=>document.querySelector("#era5CredentialFile").value===""&&!document.body.innerText.includes("SYNTHETIC-IMPORT-TOKEN")&&!JSON.stringify(ERA5_UI.state).includes("SYNTHETIC-IMPORT-TOKEN")')
                    page.locator('#era5Sync').click();wait('()=>ERA5_UI.state.job.data_only&&ERA5_UI.state.job.status==="data_ready"')
                    check('Отдельная загрузка не требует каналов и расчётчика','()=>ERA5_UI.state.job.steps.find(s=>s.key==="channels").status==="skipped"&&ERA5_UI.state.job.steps.find(s=>s.key==="engine").status==="skipped"')
                    # Only body scrolls, action and close remain above it, including mobile.
                    for width in (1440,1024,390):
                        page.set_viewport_size({'width':width,'height':844});page.locator('#era5Advanced').evaluate('(e)=>e.open=true')
                        body=page.locator('#era5Dialog .dialog-body');body.evaluate('(e)=>e.scrollTop=e.scrollHeight')
                        check('Есть реальная прокрутка '+str(width),'()=>document.querySelector("#era5Dialog .dialog-body").scrollTop>100')
                        check('Крестик закреплён и не перекрыт '+str(width),'()=>{const e=document.querySelector("#era5Dialog [data-close]"),r=e.getBoundingClientRect();return r.top>=0&&r.bottom<innerHeight&&e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}')
                        check('Основное действие доступно после прокрутки '+str(width),'()=>{const e=document.querySelector("#era5Run"),r=e.getBoundingClientRect();return r.top>=0&&r.bottom<innerHeight&&e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}')
                        check('Нет горизонтального переполнения '+str(width),'()=>document.documentElement.scrollWidth<=innerWidth+1&&document.querySelector("#era5Dialog").scrollWidth<=document.querySelector("#era5Dialog").clientWidth+1')
                        shot('scrolled-'+str(width));body.evaluate('(e)=>e.scrollTop=0');shot('top-'+str(width))
                    # Actual cancellation route, then restart of the saved workflow.
                    control['slow']=True;page.locator('#era5Sync').click();wait('()=>ERA5_UI.state.job.status==="running"&&ERA5_UI.state.job.phase.includes("тестовый источник")')
                    page.locator('#era5Cancel').click();check('Отмена имеет отдельный видимый результат','()=>ERA5_UI.state.job.status==="cancelled"')
                    control['slow']=False;page.locator('#era5Sync').click();check('Повторный запуск после отмены','()=>ERA5_UI.state.job.status==="data_ready"')
                    # Current date gets a pre-flight explanation, NOT yesterday's reanalysis.
                    page.keyboard.press('Escape');wait('()=>!document.querySelector("#era5Dialog").open')
                    now=dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=3)
                    # Change only fixture observation time in the local scene database.
                    current=app.scene(scene['id']);current['time']=now.isoformat(timespec='seconds').replace('+00:00','Z')
                    with app.store.lock,app.store.conn:app.store.conn.execute('UPDATE scenes SET data=? WHERE id=?',(json.dumps(current),scene['id']))
                    page.evaluate('(stamp)=>S.scene.time=stamp',current['time']);show()
                    check('Сегодняшний срок останавливается до сети с полезным действием','()=>ERA5_UI.preflight.next_action==="archive"&&document.querySelector("#era5Run").textContent.includes("архивный срок")')
                    check('Предлагаемая дата не подменяет срок расчёта','()=>ERA5_UI.preflight.plan.time===S.scene.time&&ERA5_UI.preflight.suggested_date!==S.scene.time.slice(0,10)')
                    shot('recent-date')
                    page.keyboard.press('Escape');check('Escape закрывает окно','()=>!document.querySelector("#era5Dialog").open')
                    if not args.bridge:
                        doc=context.new_page();response=doc.goto(base+'/docs/ERA5.html');assert response.status==200 and 'text/html' in response.headers['content-type'];doc.close()
                        report['checks'].append({'name':'Автономная HTML-справка','passed':True})
                    assert not report['browser_errors'],report['browser_errors']
                except Exception:
                    try:shot('failure');report['dom']=page.locator('#era5Next').inner_text();report['job']=app.era5_state()['job']
                    except Exception:pass
                    raise
                finally:browser.close()
        except Exception as exc:report['error']=str(exc).replace(server.key,'[SESSION]');raise
        finally:
            (out/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            app.cancel.set();server.shutdown();thread.join(3);server.server_close();app.close()
    print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
