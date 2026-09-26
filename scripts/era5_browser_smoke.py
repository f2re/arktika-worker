#!/usr/bin/env python3
"""Интерфейс ERA5 через настоящий localhost; тестовые токены не являются доступом."""
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
from server import LocalServer
from arktika.workstation import Workstation
from test_science import fixture


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='browser-results/era5');parser.add_argument('--executable');args=parser.parse_args()
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    report={'mode':'native-http','fixture':'SYNTHETIC TEST ONLY; no live credentials, no ERA5 or RTTOV scientific validation','checks':[],'browser_errors':[]}
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);fixture(root);app=Workstation(root/'state');app.scan_local(root)
        app.store.set_setting('last_date','2026-01-01');app.store.set_setting('view_settings',{'preset':'barents','product':'channel','channel':9})
        server=LocalServer(('127.0.0.1',0),app);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with sync_playwright() as pw:
                launch={'headless':True}
                if args.executable:launch['executable_path']=args.executable
                browser=pw.chromium.launch(**launch);context=browser.new_context(viewport={'width':1440,'height':960});page=context.new_page()
                page.on('pageerror',lambda e:report['browser_errors'].append(str(e)))
                def wait(expr):
                    until=time.monotonic()+30
                    while time.monotonic()<until:
                        try:
                            if page.evaluate(expr):return
                        except Exception:pass
                        page.wait_for_timeout(100)
                    raise AssertionError(expr)
                def check(name,expr):wait(expr);report['checks'].append({'name':name,'passed':True})
                try:
                    page.goto(base+'/#'+server.key)
                    wait('()=>typeof S!=="undefined"&&S.product&&!S.busy&&!UI.activeBuild')
                    page.locator('#calibrationOpen').click();page.locator('#era5Open').click()
                    check('Автокалибровка доступна из температурной шкалы','()=>document.querySelector("#era5Dialog").open')
                    check('Каналы относятся к выбранному продукту','()=>ERA5_UI.channels.join(",")==="9"')
                    check('RTTOV не скрыт за искусственной температурой','()=>document.querySelector("#era5Runtime").textContent.includes("RTTOV")')
                    page.locator('#era5CredentialFile').set_input_files({'name':'.netrc','mimeType':'text/plain','buffer':b'machine rda.ucar.edu login synthetic@example.invalid password TEST-ONLY-NOT-A-SECRET'})
                    check('netrc NCAR выбирает GDEX','()=>document.querySelector("#era5Provider").value==="gdex"')
                    check('Имя файла и пароль очищены после передачи','()=>document.querySelector("#era5CredentialFile").value===""&&!document.body.innerText.includes("TEST-ONLY-NOT-A-SECRET")')
                    assert 'TEST-ONLY-NOT-A-SECRET' not in json.dumps(app.era5_state())
                    page.locator('#era5ClearCredential').click()
                    check('Удаление доступа','()=>document.querySelector("#era5CredentialStatus").textContent.includes("не задан")')
                    page.locator('#era5CredentialFile').set_input_files({'name':'.cdsapirc','mimeType':'text/plain','buffer':b'url: https://cds.climate.copernicus.eu/api\nkey: SYNTHETIC-ACCESS-TOKEN'})
                    check('cdsapirc выбирает CDS','()=>document.querySelector("#era5Provider").value==="cds"&&document.querySelector("#era5CredentialStatus").textContent.includes("CDS")')
                    page.locator('#era5CheckPlan').click()
                    check('Запрос раскрывает уровни и переменные','()=>document.querySelector("#era5Plan").textContent.includes("37 уровнях")')
                    page.locator('#era5North').fill('91');page.locator('#era5CheckPlan').click()
                    check('Неверная область объяснена рядом с формой','()=>!document.querySelector("#era5Error").hidden')
                    page.locator('#era5North').fill('80');page.locator('#era5CheckPlan').click()
                    page.locator('#era5Run').click()
                    check('Без установленного расчётчика/зависимостей нет выдуманного результата','()=>!document.querySelector("#era5Error").hidden&&document.querySelector("#era5Report").childElementCount===0')
                    page.screenshot(path=str(out/'era5-access.png'))
                    for width in (1024,390):
                        page.set_viewport_size({'width':width,'height':844})
                        body=page.locator('#era5Dialog .dialog-body');body.evaluate('(e)=>e.scrollTop=e.scrollHeight')
                        check('Окно не переполняется '+str(width),'()=>document.documentElement.scrollWidth<=innerWidth+1&&document.querySelector("#era5Dialog").scrollWidth<=document.querySelector("#era5Dialog").clientWidth+1')
                        check('Закрытие доступно после прокрутки '+str(width),'()=>{const e=document.querySelector("#era5Dialog [data-close]"),r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}')
                        page.screenshot(path=str(out/('era5-'+str(width)+'.png')))
                    page.keyboard.press('Escape');check('Escape закрывает окно','()=>!document.querySelector("#era5Dialog").open')
                    doc=context.new_page();resp=doc.goto(base+'/docs/ERA5.html');assert resp.status==200 and 'text/html' in resp.headers['content-type'];doc.close()
                    report['checks'].append({'name':'Автономная HTML-справка доступна','passed':True})
                    assert not report['browser_errors'],report['browser_errors']
                except Exception:
                    try: page.screenshot(path=str(out/'failure.png'))
                    except Exception: pass
                    raise
                finally:browser.close()
        except Exception as exc:report['error']=str(exc).replace(server.key,'[SESSION]');raise
        finally:
            (out/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            server.shutdown();thread.join(3);server.server_close();app.close()
    print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
