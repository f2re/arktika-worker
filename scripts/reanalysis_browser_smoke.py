#!/usr/bin/env python3
"""Real browser/server checks on explicitly synthetic SOFTWARE fixtures, not weather observations."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import numpy as np
from test_reanalysis import fixture
from test_science import fixture as satellite_fixture
from arktika.geo import project_points
from arktika.workstation import Workstation
from server import LocalServer
from browser_support import mount
from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--bridge',action='store_true');parser.add_argument('--executable');parser.add_argument('--output',default='browser-results/reanalysis');args=parser.parse_args()
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    report={'mode':'explicit-http-bridge' if args.bridge else 'native-http','fixture':'SYNTHETIC SOFTWARE FIXTURES; NOT WEATHER OBSERVATIONS','checks':[],'browser_errors':[]}
    with tempfile.TemporaryDirectory() as tmp,patch('arktika.reanalysis.service.ensure_runtime',return_value={'python':sys.executable}):
        root=Path(tmp);app=Workstation(root/'state')
        a,r=fixture(value=278.15);a['temperature'].values+=np.arange(a.sizes['lon'])[None,None,None,:]*.4
        file=root/'SYNTHETIC-ERA5.nc';a.to_netcdf(file,engine='scipy')
        b,rb=fixture('merra2',value=273.15);other=root/'SYNTHETIC-MERRA2.nc';b.to_netcdf(other,engine='scipy')
        app.store.set_setting('view_settings',{'preset':'barents','product':'channel','channel':9})
        app.store.set_setting('last_date','2024-01-01')
        server=LocalServer(('127.0.0.1',0),app);threading.Thread(target=server.serve_forever,daemon=True).start()
        base='http://127.0.0.1:'+str(server.server_address[1])
        try:
            with sync_playwright() as pw:
                options={'headless':True}
                if args.executable:options['executable_path']=args.executable
                browser=pw.chromium.launch(**options);page=browser.new_page(viewport={'width':1536,'height':960})
                page.on('pageerror',lambda e:report['browser_errors'].append(str(e)))
                def wait(fn,timeout=45):
                    end=time.monotonic()+timeout
                    while time.monotonic()<end:
                        try:
                            if page.evaluate(fn):return
                        except Exception:pass
                        page.wait_for_timeout(100)
                    raise AssertionError('Browser condition failed: '+fn)
                def check(name,fn):wait(fn);report['checks'].append({'name':name,'passed':True});print(name,flush=True)
                def card():return page.locator('#fieldLayers .field-card').first
                mount(page,ROOT,server,bridge=args.bridge,scripts=('app.js','catalog.js','product_flow.js','era5.js','reanalysis.js','studio.js'))
                wait('()=>FIELD_UI.catalog&&S.map')
                page.locator('#fieldRail').click()
                check('Field flow does not require satellite', '()=>!S.product&&!document.querySelector("#reanalysisPanel").hidden')
                page.locator('#fieldTime').fill('2024-01-01T00:00')
                page.locator('#fieldLevel').select_option('850')
                page.locator('#fieldPath').evaluate('(e)=>e.closest("details").open=true')
                page.locator('#fieldPath').fill(str(file))
                page.locator('#fieldLoad').click()
                check('Local NetCDF rendered through worker and API','()=>FIELD_UI.state.layers.length===1&&document.querySelectorAll("#reanalysisOverlays image").length===1')
                wait('()=>!FIELD_UI.state.busy')
                card().locator('[data-opacity]').evaluate('(e)=>{e.value="0.30";e.dispatchEvent(new Event("input"));}');card().locator('[data-opacity]').dispatch_event('change')
                check('Opacity saved','()=>FIELD_UI.state.layers[0].style.opacity===0.3')
                page.evaluate('()=>inspectReanalysisPoint(30,70)')
                check('Native point contains source and exact time','()=>document.querySelector("#reanalysisPoint").textContent.includes("ERA5")&&document.querySelector("#reanalysisPoint").textContent.includes("2024-01-01")')
                page.evaluate('()=>tab("point")')
                page.screenshot(path=str(out/'field-native-inspector.png'))
                # Mode change does not download or change numeric field.
                card().locator('[data-mode]').select_option('contours')
                check('Contours displayed separately','()=>document.querySelectorAll("#reanalysisOverlays .field-contour").length>0&&!document.querySelector("#reanalysisOverlays image")')
                card().locator('[data-mode]').select_option('raster')
                page.locator('#fieldSource').select_option('merra2')
                page.locator('#fieldPath').fill(str(other));page.locator('#fieldLoad').click()
                check('Two independent sources','()=>FIELD_UI.state.layers.length===2&&document.querySelectorAll("#reanalysisOverlays image").length===2')
                wait('()=>!FIELD_UI.state.busy')
                page.locator('#fieldLeft').evaluate('(e)=>e.closest("details").open=true')
                ids=page.evaluate('()=>FIELD_UI.state.layers.map(l=>l.field_id)')
                page.locator('#fieldLeft').select_option(ids[0]);page.locator('#fieldRight').select_option(ids[1]);page.locator('#fieldDifference').click()
                check('Difference source added as explicit product','()=>FIELD_UI.state.layers.length===3&&FIELD_UI.state.layers.some(l=>l.source==="difference")')
                wait('()=>!FIELD_UI.state.busy')
                page.evaluate('()=>inspectReanalysisPoint(30,70)')
                check('Difference is Kelvin and not an error estimate','()=>FIELD_UI.state.layers.find(l=>l.source==="difference").units==="K"')
                page.screenshot(path=str(out/'three-layers.png'))
                layer=page.evaluate('()=>FIELD_UI.state.layers[2].id')
                card().locator('[data-down]').click()
                check('Order persisted',f'()=>FIELD_UI.state.layers[1].id==="{layer}"')
                card().locator('[data-visible]').uncheck()
                check('Visibility hides only one layer','()=>FIELD_UI.state.layers.filter(l=>l.visible).length===2&&document.querySelectorAll("#reanalysisOverlays > g").length===2')
                # Switch geographic context; overlays are regenerated, not stretched into another CRS.
                page.locator('#preset').select_option('arctic')
                check('Map CRS changed and compatible overlays rendered','()=>S.map.grid.preset==="arctic"&&FIELD_UI.state.layers.filter(l=>l.visible).every(l=>l.views["arctic:1000"])&&document.querySelectorAll("#reanalysisOverlays > g").length===2')
                # Surface average cannot be requested as an instantaneous field.
                page.locator('#fieldVariable').select_option('mslp');page.locator('#fieldTime').fill('2024-01-01T00:00');page.locator('#fieldPath').fill('')
                page.locator('#fieldLoad').click()
                check('MERRA mean timestamps not silently shifted','()=>document.querySelector("#fieldError").textContent.includes("HH:30")')
                # The same stack must survive selecting a satellite, without silently advancing field times.
                satellite=root/'SYNTHETIC-SATELLITE';satellite.mkdir();satellite_fixture(satellite);app.scan_local(satellite)
                page.evaluate('()=>setDate("2026-01-01")')
                check('Satellite and field overlays share the map','()=>S.product&&!S.busy&&document.querySelectorAll("#reanalysisOverlays > g").length===2')
                check('Changing scene does not replace pinned model time','()=>FIELD_UI.state.layers.every(l=>l.time.startsWith("2024-01-01"))')
                g=app.product_grid(app.product(page.evaluate('()=>S.product.id')))
                px,py=project_points(g,[(30,70)])[0]
                page.evaluate('(p)=>inspectAt(p[0],p[1])',[px,py])
                check('Click shows satellite plus independent reanalysis numbers','()=>UI.analysis&&document.querySelector("#reanalysisPoint").textContent.includes("MERRA-2")')
                page.screenshot(path=str(out/'satellite-and-fields.png'))
                for width,height in [(1536,960),(1024,768),(390,844)]:
                    page.set_viewport_size({'width':width,'height':height});page.evaluate('()=>left("fields")');page.screenshot(path=str(out/f'layers-{width}.png'))
                    check(f'No document overflow at {width}','()=>document.documentElement.scrollWidth<=innerWidth+2')
                self_errors=report['browser_errors'];assert not self_errors,self_errors
                browser.close()
        finally:
            app.cancel.set();server.shutdown();server.server_close()
            if app.task:app.task.join(5)
            app.close();(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'checks':len(report['checks']),'browser_errors':report['browser_errors']}))

if __name__=='__main__':main()
