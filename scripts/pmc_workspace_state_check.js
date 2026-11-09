#!/usr/bin/env node
/* Execute the production workspace state machine with controlled async I/O.
 * This checks race/error handling and automatic layer order; browser/PNG layout
 * has a separate smoke.
 */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const source=fs.readFileSync(path.join(__dirname,'../static/pmc_workspace.js'),'utf8')
  .replace(/\npmcInitWorkspace\(\);\s*$/,'');
const copy=value=>JSON.parse(JSON.stringify(value));
const GRID={preset:'barents',crs:'EPSG:3995',width:512,height:400,bounds:[-1000,-1000,1000,1000]};
const RESULT={field_id:'a'.repeat(24),source:'era5',valid_time:'2024-01-01T00:00:00Z',
  algorithm_version:'test-fixture',parameters:{},grid:{crs:GRID.crs},candidate_count:0,candidates:[]};
const MARK={id:'old-mark',x:100,y:100,lon:25,lat:72,label:'Old annotation',note:'Old evidence',status:'manual_candidate'};

function fixture(){
  const nodes=new Map();
  const element=selector=>{
    if(!nodes.has(selector))nodes.set(selector,{value:'',disabled:false,hidden:false,checked:true,inert:false,
      textContent:'',classList:{toggle(){}},focus(){},setAttribute(){},removeAttribute(){}});
    return nodes.get(selector);
  };
  const context={Promise,JSON,setTimeout,crypto:globalThis.crypto,console,
    S:{map:{grid:copy(GRID)},view:[0,0,512,400],product:null},FIELDS:{fields:[],stack:[]},
    $:element,$$:()=>[],qs:value=>new URLSearchParams(value).toString(),
    toast(){},left(){},mapClick(){},pmcClearOverlay(){},drawFieldStack(){},drawFieldCache(){},
    saveFieldStack:async()=>{},renderFieldMaps:async()=>{},ask:async()=>true,
    pmcRenderRunResult(){},pmcRunPanel(){},pmcCachedChoices(){},pmcObservationChoices(){},
    pmcInvalidateArchivePlan(){},pmcRenderSources(){},
    clearMapProduct(){context.S.product=null;},
    setMap:async(preset,width)=>{const map=await context.api('/api/map?'+new URLSearchParams({preset,width}));context.S.map=map;return map;},
    api:async()=>{throw Error('Unexpected fixture request');}};
  vm.createContext(context);vm.runInContext(source,context,{filename:'pmc_workspace.js'});
  vm.runInContext(`
    pmcSetDrawing=()=>{};pmcWorkspaceRender=()=>{};left=()=>{};
    pmcWorkspaceSnapshot=()=>pmcCopy({schema_version:'arktika.pmc_workspace.v1',map:{...S.map.grid,view:S.view},
      annotations:PMCW.annotations,result:PMCW.result,field_stack:FIELDS.stack});
  `,context);
  context.initialResult=copy(RESULT);context.initialMark=copy(MARK);
  vm.runInContext(`Object.assign(PMCW,{result:initialResult,annotations:[initialMark],annotationGrid:S.map.grid,
    bookmarkId:'bookmark-a',dirty:true});`,context);
  element('#pmcWorkspaceTitle').value='Analysis A';element('#pmcWorkspaceNotes').value='Note before save';
  element('#pmcAnnotationLabel').value=MARK.label;element('#pmcAnnotationNote').value=MARK.note;
  element('#pmcAnnotationStatus').value=MARK.status;
  return {context,element,run:code=>vm.runInContext(code,context),state:()=>copy(vm.runInContext('PMCW',context))};
}

function bookmark(id='bookmark-b'){
  return {bookmark:{id,title:'Analysis B',notes:'Saved B notes',state:{schema_version:'arktika.pmc_workspace.v1',
    map:{...copy(GRID),view:[10,20,256,200]},product_id:null,field_stack:[],result:copy(RESULT),
    annotations:[{...copy(MARK),id:'new-mark',label:'B annotation'}],raster_visible:false,night_visible:false}}};
}

function restoreApi(f,{failMapCall=0}={}){
  let mapCalls=0;
  return async url=>{
    if(url.startsWith('/api/pmc/bookmark?'))return bookmark();
    if(url==='/api/reanalysis/state')return {fields:[],catalogue:{sources:[]}};
    if(url==='/api/pmc/observations')return {observations:[]};
    if(url.startsWith('/api/map?')){
      mapCalls++;
      if(mapCalls===failMapCall)throw Error('Injected map I/O failure');
      return {grid:copy(GRID)};
    }
    throw Error('Unexpected fixture request: '+url);
  };
}

async function delayedSaveKeepsNewEdits(){
  const f=fixture();let release,stored;
  f.context.api=async(url,body)=>{assert.equal(url,'/api/pmc/bookmarks/save');stored=copy(body);return new Promise(resolve=>release=resolve);};
  const pending=f.run('pmcSaveBookmark(false)');
  assert.equal(typeof release,'function');
  f.element('#pmcWorkspaceNotes').value='New unsaved note';f.run('pmcWorkspaceDirty()');
  release({bookmark:{id:'bookmark-a',title:'Analysis A'}});await pending;
  assert.equal(stored.notes,'Note before save');
  assert.equal(f.element('#pmcWorkspaceNotes').value,'New unsaved note');
  assert.equal(f.state().dirty,true,'A delayed save must not declare later edits saved');
}

async function staleSaveDoesNotRebindAnotherBookmarkOfSameResult(){
  const f=fixture();let release;
  const api=restoreApi(f);
  f.context.api=async(url,body)=>url==='/api/pmc/bookmarks/save'?new Promise(resolve=>release=resolve):api(url,body);
  const pending=f.run('pmcSaveBookmark(false)');
  await f.run("pmcRestoreBookmark('bookmark-b')");
  assert.deepEqual(f.state().result,RESULT,'Both bookmarks deliberately contain the same numerical result');
  release({bookmark:{id:'bookmark-a',title:'Analysis A'}});await pending;
  assert.equal(f.state().bookmarkId,'bookmark-b','The old response must not replace the restored bookmark ID');
  assert.equal(f.element('#pmcWorkspaceTitle').value,'Analysis B');
  assert.equal(f.element('#pmcWorkspaceNotes').value,'Saved B notes');
}

async function restoreFailurePreservesWork(failMapCall){
  const f=fixture(),before=f.state();
  f.context.api=restoreApi(f,{failMapCall});
  await assert.rejects(f.run("pmcRestoreBookmark('bookmark-b')"),/Injected map I\/O failure/);
  for(const key of ['result','annotations','bookmarkId','dirty'])assert.deepEqual(f.state()[key],before[key],key+' must survive failed restore');
  assert.equal(f.element('#pmcWorkspaceTitle').value,'Analysis A');
  assert.equal(f.element('#pmcWorkspaceNotes').value,'Note before save');
  assert.equal(f.element('#shell').inert,false);
  assert.equal(f.state().restoring,false);
}

async function concurrentRestoreIsRejected(){
  const f=fixture();let release;
  const api=restoreApi(f);
  f.context.api=async url=>url.startsWith('/api/pmc/bookmark?')?new Promise(resolve=>release=resolve):api(url);
  const pending=f.run("pmcRestoreBookmark('bookmark-b')");
  await assert.rejects(f.run("pmcRestoreBookmark('bookmark-c')"),/уже открывается/);
  release(bookmark());await pending;
  assert.equal(f.state().bookmarkId,'bookmark-b');
}

async function contextStackKeepsPressureOverWind(){
  const f=fixture();
  f.context.FIELDS.fields=[
    {id:'pressure',source:RESULT.source,time:RESULT.valid_time,variable:'mslp',level:null},
    {id:'wind',source:RESULT.source,time:RESULT.valid_time,variable:'wind',level:850},
  ];
  await f.run('pmcShowContextFields()');
  assert.deepEqual(copy(f.context.FIELDS.stack).map(layer=>layer.id),['wind','pressure'],
    'Cache order must not let a later wind fill obscure pressure contours');
  assert.equal(f.context.FIELDS.stack[1].style,'contours');
}

(async()=>{
  for(const [name,check] of [
    ['late save preserves newer edits',delayedSaveKeepsNewEdits],
    ['late save cannot rebind another bookmark of the same result',staleSaveDoesNotRebindAnotherBookmarkOfSameResult],
    ['failed restore preflight preserves work',()=>restoreFailurePreservesWork(1)],
    ['failed restore after mutation rolls back work',()=>restoreFailurePreservesWork(2)],
    ['concurrent restore is rejected',concurrentRestoreIsRejected],
    ['automatic stack keeps pressure over wind independently of cache order',contextStackKeepsPressureOverWind],
  ]){await check();console.log('PASS '+name);}
})().catch(error=>{console.error(error);process.exitCode=1;});
