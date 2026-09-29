/* Полевые слои независимы от спутниковой радиометрии. Сроки не подменяются. */
'use strict';
const FIELD_UI={catalog:null,state:null,timer:null,refreshing:false,signature:'',renderCache:new Map(),generation:0,pointSerial:0,lastPoint:null,failedMap:'',requestingMap:false,initialised:false};
function fieldError(message='') { const p=$('#fieldError');if(p){p.textContent=message;p.hidden=!message;} }
function fieldMapKey(){return S.map?S.map.grid.preset+':'+S.map.grid.width:'';}
function fieldMapArgs(){return {preset:S.map?.grid.preset||$('#preset').value||'arctic',width:S.map?.grid.width||1000};}
function fieldTimeText(row){return row.time.replace('T',' ').replace('Z',' UTC')+(row.temporal==='mean_1h'?' · среднее за час':' · анализ');}
function fieldTitle(row){return row.name+' · '+row.title+(row.level?' · '+row.level+' гПа':'')+' · '+fieldTimeText(row);}
function fieldSpec(){return FIELD_UI.catalog?.sources.find(s=>s.id===$('#fieldSource').value)?.fields.find(f=>f.id===$('#fieldVariable').value);}
function fieldControls(reset=false){
  const source=FIELD_UI.catalog.sources.find(s=>s.id===$('#fieldSource').value),old=$('#fieldVariable').value;
  $('#fieldVariable').innerHTML=source.fields.map(f=>`<option value="${escape(f.id)}">${escape(f.name)}</option>`).join('');
  if(source.fields.some(f=>f.id===old))$('#fieldVariable').value=old;
  const f=fieldSpec(),level=$('#fieldLevel').value||'850';
  $('#fieldLevel').innerHTML=f.levels.map(l=>`<option value="${l}">${l} гПа</option>`).join('');
  if(f.levels.includes(Number(level)))$('#fieldLevel').value=level;
  $('#fieldLevelLabel').hidden=f.vertical!=='pressure';
  $('#fieldTimeNote').textContent=f.minute===30?'MERRA-2: часовые средние, срок в середине интервала — HH:30 UTC. Не вычитаются из мгновенных полей.':`Исходные сроки анализа: каждые ${f.hour_step} ч, HH:00 UTC. Соседний срок не подставляется.`;
  $('#fieldSourceNote').textContent=source.id==='carra2'?'CARRA2: доступность года проверяется по живому каталогу CDS. Это архивный реанализ, не оперативный прогноз.':source.id==='merra2'?'Для облачного OPeNDAP нужны разрешения Earthdata для GES DISC / Hyrax.':'ERA5 запрашивается как самостоятельное поле; RTTOV не требуется.';
}
function fieldInput(){
  const f=fieldSpec(),time=$('#fieldTime').value;
  if(!time)throw Error('Укажите срок поля UTC.');
  return {...fieldMapArgs(),source:$('#fieldSource').value,field:f.id,level:f.vertical==='pressure'?Number($('#fieldLevel').value):null,
    time:time+(time.length===16?':00Z':'Z'),area:['#fieldNorth','#fieldWest','#fieldSouth','#fieldEast'].map(id=>$(id).value===''?null:Number($(id).value)),
    path:$('#fieldPath').value.trim()||undefined,refresh:$('#fieldRefresh').checked};
}
function fieldAreaFromMap(){
  const p=S.registry?.presets?.[fieldMapArgs().preset];
  const b=p?.geo_bounds||[-180,40,180,90];
  for(const [id,value] of [['#fieldNorth',b[3]],['#fieldWest',b[0]],['#fieldSouth',b[1]],['#fieldEast',b[2]]])$(id).value=value;
}
function fieldAction(fn){return async()=>{fieldError();try{await fn();FIELD_UI.failedMap='';await refreshFields();}catch(e){fieldError(e.message);}};}
async function fieldLayerUpdate(id,body){
  await api('/api/reanalysis/layers',{id,action:'update',...body});FIELD_UI.failedMap='';invalidateReanalysisPoint();await refreshFields();
}
function drawFieldCards(state){
  const openIds=new Set($$('#fieldLayers details[open]').map(e=>e.dataset.layer));
  $('#fieldLayers').innerHTML=state.layers.length?[...state.layers].reverse().map(l=>{
    const options=l.style,view=l.views[fieldMapKey()];
    const mismatch=S.scene&&l.time!==S.scene.time;
    return `<article class="field-card" data-layer="${escape(l.id)}"><div class="field-card-heading"><label class="check"><input type="checkbox" data-visible ${l.visible?'checked':''}><strong>${escape(l.name+' · '+l.title)}</strong></label><button class="text-button" data-remove aria-label="Убрать слой ${escape(l.title)}">×</button></div>
    <p class="micro">${l.level?l.level+' гПа · ':''}${escape(fieldTimeText(l))}<br>Шаг источника: ${num(l.resolution_km)} км${mismatch?' · срок отличается от снимка':''}</p>
    <label>Отображение<select data-mode><option value="raster" ${options.mode==='raster'?'selected':''}>Заливка</option><option value="contours" ${options.mode==='contours'?'selected':''}>Изолинии</option>${l.vector?`<option value="wind" ${options.mode==='wind'?'selected':''}>Векторы ветра</option>`:''}</select></label>
    ${options.mode==='wind'?'<p class="micro">Стрелки показывают направление переноса. Скорость — в анализе точки.</p>':''}<label class="field-opacity">Непрозрачность<input data-opacity type="range" min="0" max="1" step="0.05" value="${options.opacity}"></label>
    <div class="field-legend"><span>${num(options.min)} ${escape(l.units)}</span><span>${num(options.max)} ${escape(l.units)}</span></div><div class="field-palette" data-palette></div>
    <details data-layer="${escape(l.id)}" ${openIds.has(l.id)?'open':''}><summary>Шкала и файлы</summary><div class="row"><label>От<input data-min type="number" step="any" value="${options.min}"></label><label>До<input data-max type="number" step="any" value="${options.max}"></label></div><label>Шаг изолиний<input data-step type="number" step="any" min="0.0001" value="${options.step}"></label><button data-scale class="tonal">Применить шкалу</button><div class="row wrap"><a href="/reanalysis/field/${l.field_id}/field.nc" download>NetCDF</a><a href="/reanalysis/field/${l.field_id}/field.json" download>Происхождение</a>${view?`<a href="/reanalysis/render/${view.id}/values.tif" download>GeoTIFF</a>`:''}</div></details>
    <div class="row"><button data-up class="text-button" aria-label="Выше">↑ Выше</button><button data-down class="text-button" aria-label="Ниже">↓ Ниже</button><small class="micro">${l.visible&&!view?'Ожидает построения':view?.valid_pixels===0?'Нет данных в текущей области':''}</small></div></article>`;
  }).join(''):'<p class="hint">Добавьте поле реанализа. Спутниковый снимок для этого не нужен.</p>';
  for(const card of $$('#fieldLayers .field-card')){
    const id=card.dataset.layer;
    card.querySelector('[data-visible]').onchange=fieldAction(()=>fieldLayerUpdate(id,{visible:card.querySelector('[data-visible]').checked}));
    card.querySelector('[data-mode]').onchange=fieldAction(()=>fieldLayerUpdate(id,{style:{mode:card.querySelector('[data-mode]').value}}));
    card.querySelector('[data-opacity]').oninput=e=>{const g=$(`#reanalysisOverlays [data-layer="${id}"]`);if(g)g.setAttribute('opacity',e.target.value);};
    card.querySelector('[data-opacity]').onchange=fieldAction(()=>fieldLayerUpdate(id,{style:{opacity:Number(card.querySelector('[data-opacity]').value)}}));
    card.querySelector('[data-scale]').onclick=fieldAction(()=>fieldLayerUpdate(id,{style:Object.fromEntries(['min','max','step'].map(k=>[k,card.querySelector(`[data-${k}]`).value===''?null:Number(card.querySelector(`[data-${k}]`).value)]))}));
    card.querySelector('[data-remove]').onclick=fieldAction(async()=>{await api('/api/reanalysis/layers',{action:'remove',id});invalidateReanalysisPoint();});
    for(const [name,step] of [['up',1],['down',-1]])card.querySelector(`[data-${name}]`).onclick=fieldAction(()=>api('/api/reanalysis/layers',{action:'move',id,step}));
  }
  const fields=state.fields;
  for(const selector of ['#fieldCached','#fieldLeft','#fieldRight']){
    const e=$(selector),old=e.value,rows=selector==='#fieldCached'?fields:fields.filter(f=>!f.difference&&!f.vector);
    e.innerHTML='<option value="">Выберите поле</option>'+rows.map(f=>`<option value="${f.id}">${escape(fieldTitle(f))}</option>`).join('');
    if(rows.some(f=>f.id===old))e.value=old;
  }
  $('#fieldCacheInfo').textContent=fields.length+' полей · '+size(state.cache_bytes);
}
async function drawFieldMap(){
  if(!FIELD_UI.initialised||!S.map||!FIELD_UI.state)return;
  const generation=++FIELD_UI.generation,key=fieldMapKey(),grid=S.map.grid;
  $('#reanalysisOverlays').replaceChildren();
  for(const l of FIELD_UI.state.layers){
    const view=l.views[key];if(!l.visible||!view||view.crs!==grid.crs||JSON.stringify(view.bounds)!==JSON.stringify(grid.bounds))continue;
    let r=FIELD_UI.renderCache.get(view.id);
    if(!r){r=await api(`/reanalysis/render/${view.id}/render.json`);if(FIELD_UI.renderCache.size>24)FIELD_UI.renderCache.clear();FIELD_UI.renderCache.set(view.id,r);}
    if(generation!==FIELD_UI.generation||key!==fieldMapKey())return;
    const group=svgEl('g',{'data-layer':l.id,opacity:l.style.opacity,'pointer-events':'none'});
    if(l.style.mode==='raster')group.append(svgEl('image',{href:r.image,width:r.width,height:r.height}));
    if(l.style.mode==='contours')for(const [i,line] of r.contours.entries()){
      group.append(svgEl('polyline',{points:line.points.map(p=>p.join(',')).join(' '),class:'field-contour'}));
      if(line.points.length>40&&i%3===0){const p=line.points[Math.floor(line.points.length/2)],t=svgEl('text',{x:p[0],y:p[1],class:'field-contour-label'});t.textContent=num(line.value,1);group.append(t);}
    }
    if(l.style.mode==='wind')for(const v of r.vectors){
      const x=v.x+v.dx,y=v.y+v.dy,n=Math.hypot(v.dx,v.dy),ux=v.dx/n,uy=v.dy/n;
      const p=`M${v.x},${v.y} L${x},${y} M${x-ux*6-uy*3},${y-uy*6+ux*3} L${x},${y} L${x-ux*6+uy*3},${y-uy*6-ux*3}`;
      const arrow=svgEl('path',{d:p,class:'field-wind'}),title=svgEl('title',{});title.textContent=v.speed+' м/с';arrow.append(title);group.append(arrow);
    }
    $('#reanalysisOverlays').append(group);
    const palette=$(`#fieldLayers [data-layer="${l.id}"] [data-palette]`);
    if(palette)palette.style.background='linear-gradient(to right,'+r.legend.colors.map(c=>'rgb('+c.join(',')+')').join(',')+')';
  }
}
async function fieldEnsureMap(){
  const state=FIELD_UI.state,key=fieldMapKey();if(!state||!key||state.busy||FIELD_UI.requestingMap)return;
  const missing=state.layers.filter(l=>l.visible&&!l.views[key]);if(!missing.length)return;
  const signature=key+'|'+missing.map(l=>l.id+JSON.stringify(l.style)).join('|');
  if(FIELD_UI.failedMap===signature)return;
  FIELD_UI.requestingMap=true;FIELD_UI.failedMap=signature;
  try{await api('/api/reanalysis/map',fieldMapArgs());}catch(e){fieldError(e.message);}finally{FIELD_UI.requestingMap=false;}
}
async function refreshFields(){
  if(!FIELD_UI.initialised||FIELD_UI.refreshing)return;
  FIELD_UI.refreshing=true;
  try{
    const state=await api('/api/reanalysis/state');FIELD_UI.state=state;
    const signature=JSON.stringify([state.layers,state.fields.map(f=>f.id),fieldMapKey(),S.scene?.time]);
    if(signature!==FIELD_UI.signature){FIELD_UI.signature=signature;drawFieldCards(state);await drawFieldMap();}
    $('#fieldJob').textContent=state.job.message||'';$('#fieldJob').classList.toggle('error-text',state.job.status==='error');
    $('#fieldCancel').hidden=state.job.status!=='running';
    for(const id of ['#fieldLoad','#fieldUseCache','#fieldDifference'])$(id).disabled=state.busy;
    await fieldEnsureMap();
  }catch(e){fieldError(e.message);}finally{FIELD_UI.refreshing=false;}
}
function reanalysisMapChanged(){
  if(!FIELD_UI.initialised)return;
  FIELD_UI.generation++;FIELD_UI.signature='';FIELD_UI.failedMap='';$('#reanalysisOverlays').replaceChildren();
  invalidateReanalysisPoint();refreshFields();
}
function invalidateReanalysisPoint(){
  FIELD_UI.pointSerial++;FIELD_UI.lastPoint=null;const p=$('#reanalysisPoint');if(p){p.replaceChildren();p.hidden=true;}
}
async function inspectReanalysisPoint(lon,lat){
  if(!FIELD_UI.initialised||!FIELD_UI.state?.layers.some(l=>l.visible))return;
  const serial=++FIELD_UI.pointSerial;FIELD_UI.lastPoint={lon,lat};if(!S.product)$('#pixel').innerHTML=`<h3>${num(lat,3)}° · ${num(lon,3)}°</h3>`;const target=$('#reanalysisPoint');target.hidden=false;target.textContent='Чтение исходных узлов реанализа…';
  try{
    const r=await api('/api/reanalysis/point',{lon,lat});if(serial!==FIELD_UI.pointSerial)return;
    target.innerHTML='<h3>Реанализы в точке</h3>'+r.fields.map(f=>`<article class="field-point"><strong>${escape(f.name+' · '+f.title)}</strong><p>${num(f.value,2)} ${escape(f.units)}${f.level?' · '+f.level+' гПа':''}</p><small>${escape(fieldTimeText(f))}</small>${f.value===null?'<p class="hint">Нет данных в области/узле. Не интерпретируется как ноль.</p>':`<p class="micro">Узел ${num(f.native_lat,3)}°, ${num(f.native_lon,3)}° · ${num(f.distance_km,2)} км от точки${f.u!==undefined?'<br>u = '+num(f.u)+'; v = '+num(f.v)+' м/с':''}</p>`}</article>`).join('')+'<p class="micro">'+escape(r.note)+'</p>';
  }catch(e){if(serial===FIELD_UI.pointSerial)target.textContent=e.message;}
}
async function inspectFieldOnly(x,y){
  invalidatePoint();const serial=FIELD_UI.pointSerial,key=fieldMapKey();
  const p=await api('/api/coordinates',{...fieldMapArgs(),x,y});if(serial!==FIELD_UI.pointSerial||key!==fieldMapKey())return;
  tab('point');$('#pixel').innerHTML=`<h3>${num(p.lat,3)}° · ${num(p.lon,3)}°</h3>`;
  $('#pointLayer').replaceChildren(svgEl('circle',{cx:x,cy:y,r:5,fill:'none',stroke:'white','stroke-width':2}));
  await inspectReanalysisPoint(p.lon,p.lat);
}
async function openFieldSource(source){
  if($('#sourcesDialog').open)$('#sourcesDialog').close();left('fields');
  if(!FIELD_UI.catalog)return;
  $('#fieldSource').value=source;fieldControls();$('#fieldAdd').open=true;
}
function initReanalysisUI(){
  if(FIELD_UI.initialised)return;FIELD_UI.initialised=true;
  const overlay=svgEl('g',{id:'reanalysisOverlays','pointer-events':'none'});$('#coast').before(overlay);
  const point=document.createElement('section');point.id='reanalysisPoint';point.hidden=true;$('#tab-point').append(point);
  $('#fieldRail').onclick=()=>{left('fields',false);refreshFields();};
  $('#fieldSource').onchange=()=>fieldControls();$('#fieldVariable').onchange=()=>fieldControls();
  $('#fieldSceneTime').onclick=()=>{if(!S.scene){fieldError('Сначала выберите спутниковый срок либо введите архивный срок вручную.');return;}$('#fieldTime').value=S.scene.time.replace('Z','').slice(0,19);fieldError();};
  $('#fieldMapArea').onclick=fieldAreaFromMap;
  $('#fieldLoad').onclick=fieldAction(async()=>{await api('/api/reanalysis/prepare',fieldInput());$('#fieldJob').textContent='Подготовка поля…';});
  $('#fieldUseCache').onclick=fieldAction(()=>api('/api/reanalysis/layers',{action:'cached',field_id:$('#fieldCached').value,...fieldMapArgs()}));
  $('#fieldDifference').onclick=fieldAction(()=>api('/api/reanalysis/difference',{left:$('#fieldLeft').value,right:$('#fieldRight').value,...fieldMapArgs()}));
  $('#fieldCancel').onclick=fieldAction(()=>api('/api/reanalysis/cancel',{id:FIELD_UI.state?.job.id}));
  $('#fieldRetry').onclick=()=>{FIELD_UI.failedMap='';refreshFields();};
  $('#fieldAccess').onclick=fieldAction(()=>openSourceAccess($('#fieldSource').value));
  api('/api/reanalysis/catalog').then(c=>{
    FIELD_UI.catalog=c;$('#fieldSource').innerHTML=c.sources.map(s=>`<option value="${s.id}">${escape(s.name)}</option>`).join('');fieldControls();
    if(S.scene)$('#fieldTime').value=S.scene.time.replace('Z','').slice(0,19);
    refreshFields();FIELD_UI.timer=setInterval(refreshFields,1800);
  }).catch(e=>fieldError(e.message));
}
