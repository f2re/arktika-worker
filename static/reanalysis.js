/* Пространственные поля: независимый от спутника стек, точные сроки, без CDN. */
'use strict';
const FIELDS={catalogue:[],fields:[],stack:[],epoch:0,probeEpoch:0,pending:null,timer:null,saveTail:Promise.resolve(),job:{status:'idle'}};
const fieldDialog=document.createElement('dialog');fieldDialog.id='fieldDialog';fieldDialog.className='wide fixed-dialog';
fieldDialog.innerHTML=`<div class="dialog-head"><div><h2>Добавить поле реанализа</h2><p>Один источник · один срок анализа · один уровень</p></div><button id="fieldClose" aria-label="Закрыть">✕</button></div>
<div class="dialog-body"><div class="row"><label>Источник<select id="fieldSource"></select></label><label>Поле<select id="fieldVariable"></select></label></div>
<div class="row"><label id="fieldLevelLabel">Уровень, гПа<select id="fieldLevel"></select></label><label>Срок анализа, UTC<input id="fieldTime" type="datetime-local" step="3600"></label></div>
<p id="fieldTimeHelp" class="hint"></p><div class="row"><p id="fieldAccessHelp" class="hint"></p><button id="fieldConfigure" class="text-button">Настроить доступ</button></div>
<details id="fieldArea"><summary>Область запроса</summary><div class="row"><label>Север, °<input id="fieldNorth" type="number" min="-90" max="90" value="82" step="any"></label><label>Юг, °<input id="fieldSouth" type="number" min="-90" max="90" value="60" step="any"></label></div><div class="row"><label>Запад, °<input id="fieldWest" type="number" min="-180" max="180" value="-20" step="any"></label><label>Восток, °<input id="fieldEast" type="number" min="-180" max="180" value="70" step="any"></label></div><button id="fieldUseRegion" class="text-button">Границы выбранной области карты</button><p class="micro">Для пересечения 180° задайте запад больше востока. Запрос делится на две части, без подмены области.</p></details>
<details><summary>Импорт локального NetCDF / GRIB</summary><label>Полный путь к файлу на компьютере сервера<input id="fieldPath" placeholder="/data/reanalysis/field.nc" autocomplete="off"></label><p class="micro">Используются выбранные выше источник, поле, уровень и срок. Единицы и координаты проверяются по файлу. Исходник не изменяется.</p><button id="fieldImport" class="tonal">Проверить и импортировать</button></details>
<p id="fieldError" class="error-text" role="alert" hidden></p><p id="fieldJobMessage" role="status"></p>
<details id="fieldCacheDetails"><summary>Уже загруженные поля</summary><div id="fieldCache"></div></details>
<details id="fieldDifferenceDetails"><summary>Разность двух источников: A − B</summary><div class="row"><label>A<select id="fieldFirst"></select></label><label>B<select id="fieldSecond"></select></label></div><p class="micro">Одинаковые поле, уровень и срок. Расчёт на более грубой исходной сетке; вне совместной маски данных результата нет.</p><button id="fieldDifference" class="tonal">Рассчитать разность</button></details>
</div><div class="dialog-footer"><a href="/docs/REANALYSIS.html" target="_blank" rel="noopener">Поля и ограничения</a><button id="fieldDownload" class="primary">Загрузить и добавить на карту</button></div>`;
document.body.append(fieldDialog);
function fieldError(message=''){const el=$('#fieldError');el.textContent=message;el.hidden=!message;}
function fieldMeta(id){return FIELDS.fields.find(f=>f.id===id);}
function fieldNumber(v){return num(v!==null&&Math.abs(v)<.005?0:v,2);}
function fieldLabel(f){return `${f.source_label||f.source} · ${f.title} ${f.level===null?'':f.level+' гПа'} · ${f.time.replace('T',' ').replace('Z',' UTC')}`;}
function fieldStyleOptions(f){return f.wind?[['fill','Заливка'],['arrows','Ветер'],['fill_arrows','Заливка и ветер']]:[['fill','Заливка'],['contours','Изолинии'],['fill_contours','Заливка и изолинии']];}
function timeMismatch(f){const t=S.product?.time||S.scene?.time;if(!t)return FIELDS.stack.filter(s=>s.visible).some(s=>fieldMeta(s.id)?.time!==f.time)?'На карте поля разных сроков':'';if(t===f.time)return '';const gap=(Date.parse(f.time)-Date.parse(t))/3600000;return `Срок отличается от снимка: ${gap>0?'+':''}${num(gap,2)} ч`;}
function fieldSelectors(){
  const source=FIELDS.catalogue.find(s=>s.id===$('#fieldSource').value);if(!source)return;
  const previous=$('#fieldVariable').value;
  $('#fieldVariable').innerHTML=source.variables.map(v=>`<option value="${v.id}">${escape(v.title)}</option>`).join('');
  if(source.variables.some(v=>v.id===previous))$('#fieldVariable').value=previous;
  fieldLevels();
}
function fieldLevels(){
  const source=FIELDS.catalogue.find(s=>s.id===$('#fieldSource').value),v=source?.variables.find(v=>v.id===$('#fieldVariable').value);if(!v)return;
  const old=$('#fieldLevel').value;
  $('#fieldLevelLabel').hidden=v.vertical!=='pressure';$('#fieldLevel').innerHTML=v.levels.map(x=>`<option value="${x}">${x}</option>`).join('');
  $('#fieldLevel').value=v.levels.includes(Number(old))?old:v.levels.includes(850)?'850':String(v.levels[0]??'');
  const step=source.id==='merra2'&&v.vertical==='surface'?1:source.step_hours;
  $('#fieldTimeHelp').textContent=`${source.name}: анализ через ${step} ч. Время не округляется, ближайший срок не подставляется.`;
  $('#fieldAccessHelp').textContent=source.auth==='cds'?'Доступ: Copernicus CDS, общий с ERA5/CARRA2. Условия конкретного набора принимаются на сайте CDS.':'Доступ: NASA Earthdata. MERRA-2 загружается поднабором OPeNDAP, без скачивания всех уровней за сутки.';
}
function fieldBody(){
  const source=$('#fieldSource').value,variable=$('#fieldVariable').value;
  const raw=$('#fieldTime').value;if(!raw)throw Error('Укажите срок анализа в UTC.');
  const area=['#fieldNorth','#fieldWest','#fieldSouth','#fieldEast'].map(id=>{if($(id).value==='')throw Error('Заполните границы области.');return Number($(id).value);});
  return {source,variable,time:raw+(raw.length===16?':00Z':'Z'),level:$('#fieldLevelLabel').hidden?null:Number($('#fieldLevel').value),area};
}
function fieldsJob(){
  const j=FIELDS.job,busy=j.status==='running';
  $('#fieldJobMessage').textContent=busy?(j.message||''):'';
  if($('#fieldStatus'))$('#fieldStatus').textContent=j.message||'';
  if($('#fieldCancel')){$('#fieldCancel').hidden=!busy;$('#fieldCancel').disabled=!busy;}
  for(const id of ['#fieldDownload','#fieldImport','#fieldDifference','#fieldConfigure'])$(id).disabled=busy;
  if(j.status==='error'||j.status==='interrupted')fieldError(j.message);
}
async function refreshFields(){
  const data=await api('/api/reanalysis/state');FIELDS.catalogue=data.catalogue.sources;FIELDS.fields=data.fields;FIELDS.job=data.job;
  if(!$('#fieldSource').options.length){$('#fieldSource').innerHTML=FIELDS.catalogue.map(s=>`<option value="${s.id}">${escape(s.name)}</option>`).join('');fieldSelectors();}
  fieldsJob();drawFieldCache();
  const legacy=$('#fieldLegacyNotice');
  if(legacy){legacy.hidden=!data.legacy_cache_count;legacy.textContent=data.legacy_cache_count?'Кэш предыдущей версии сохранён на диске. Получите поля повторно: изменились проверки времени и формат метаданных.':'';}
  if(FIELDS.pending&&data.job.id===FIELDS.pending&&data.job.status==='done'){
    FIELDS.pending=null;await addCachedField(data.job.field_id);toast('Поле добавлено на карту. Его срок указан в списке слоёв.');
  }
  if(!['running'].includes(data.job.status)){clearTimeout(FIELDS.timer);FIELDS.timer=null;}
  return data;
}
async function pollFields(){
  try{await refreshFields();}catch(e){fieldError(e.message);}
  if(FIELDS.job.status==='running')FIELDS.timer=setTimeout(pollFields,1200);
}
async function openFieldDialog(source){
  fieldError();await refreshFields();
  if(source){$('#fieldSource').value=source;fieldSelectors();}
  if(!$('#fieldTime').value)$('#fieldTime').value=(S.product?.time||S.scene?.time||S.day+'T00:00:00Z').slice(0,16);
  if($('#sourcesDialog').open)$('#sourcesDialog').close();
  if(!fieldDialog.open)fieldDialog.showModal();
}
function drawFieldCache(){
  $('#fieldCache').innerHTML=FIELDS.fields.length?FIELDS.fields.map(f=>`<article class="field-cache-row"><span>${escape(fieldLabel(f))}</span><button class="tonal" data-field-add="${f.id}" ${FIELDS.stack.some(s=>s.id===f.id)?'disabled':''}>Добавить</button></article>`).join(''):'<p class="hint">Пока нет сохранённых полей.</p>';
  $$('#fieldCache [data-field-add]').forEach(b=>b.onclick=()=>addCachedField(b.dataset.fieldAdd).catch(e=>fieldError(e.message)));
  const originals=FIELDS.fields.filter(f=>!f.difference_of);
  for(const selector of ['#fieldFirst','#fieldSecond']){const old=$(selector).value;$(selector).innerHTML=originals.map(f=>`<option value="${f.id}">${escape(fieldLabel(f))}</option>`).join('');if(originals.some(f=>f.id===old))$(selector).value=old;}
  if($('#fieldFirst').value===$('#fieldSecond').value&&originals.length>1)$('#fieldSecond').selectedIndex=1;
}
async function startField(local=false){
  fieldError();try{
    const body=fieldBody();if(local)body.path=$('#fieldPath').value;
    const result=await api('/api/reanalysis/'+(local?'import':'add'),body);
    FIELDS.pending=result.id;FIELDS.job={id:result.id,status:'running',message:'Проверка и подготовка поля'};fieldsJob();
    clearTimeout(FIELDS.timer);await pollFields();
  }catch(e){fieldError(e.message);}
}
function saveFieldStack(){
  const snapshot=FIELDS.stack.map(x=>({...x}));
  FIELDS.saveTail=FIELDS.saveTail.catch(()=>{}).then(()=>api('/api/reanalysis/stack',{stack:snapshot}));
  return FIELDS.saveTail;
}
async function addCachedField(id){
  if(FIELDS.stack.some(x=>x.id===id))return;
  const f=fieldMeta(id);if(!f)throw Error('Поле не найдено. Обновите список.');
  if(FIELDS.stack.length>=8)throw Error('Снимите один из восьми слоёв перед добавлением.');
  FIELDS.stack.push({id,opacity:.65,style:f.wind?'fill_arrows':f.variable==='mslp'?'contours':'fill',visible:true});
  await saveFieldStack();drawFieldStack();drawFieldCache();renderFieldMaps();
}
function drawFieldStack(){
  if(!$('#fieldLayerRows'))return;
  $('#fieldLayerRows').innerHTML=FIELDS.stack.map((s,i)=>{
    const f=fieldMeta(s.id);if(!f)return '';const mismatch=timeMismatch(f);
    return `<article class="field-layer" data-layer="${s.id}"><div class="field-layer-heading"><label class="check"><input type="checkbox" data-field-visible="${s.id}" ${s.visible?'checked':''}><strong>${escape(f.source_label||f.source)}</strong></label><button class="text-button" data-field-up="${s.id}" ${i===FIELDS.stack.length-1?'disabled':''} aria-label="Выше в наложении">↑</button><button class="text-button" data-field-remove="${s.id}" aria-label="Убрать слой">✕</button></div><p>${escape(f.title)} ${f.level===null?'':escape(f.level)+' гПа'}</p><p class="micro">${escape(f.time.replace('T',' ').replace('Z',' UTC'))}</p>${mismatch?`<p class="field-time-warning">${escape(mismatch)}</p>`:''}<div class="row"><select data-field-style="${s.id}" aria-label="Отображение слоя">${fieldStyleOptions(f).map(([key,name])=>`<option value="${key}" ${s.style===key?'selected':''}>${name}</option>`).join('')}</select><a class="text-button" href="/field-export/${s.id}" download>NetCDF + отчёт</a></div><label class="field-opacity">Непрозрачность<input data-field-opacity="${s.id}" aria-label="Непрозрачность слоя" type="range" min="0" max="1" step=".05" value="${s.opacity}"></label><p class="field-render-error" role="status"></p><div class="field-legend"></div></article>`;
  }).join('')||'<p class="hint">Добавьте давление, температуру или ветер поверх спутника — либо работайте только с реанализом.</p>';
  $$('#fieldLayerRows [data-field-visible]').forEach(e=>e.onchange=()=>{FIELDS.stack.find(s=>s.id===e.dataset.fieldVisible).visible=e.checked;stackChanged();});
  $$('#fieldLayerRows [data-field-style]').forEach(e=>e.onchange=()=>{FIELDS.stack.find(s=>s.id===e.dataset.fieldStyle).style=e.value;stackChanged();});
  $$('#fieldLayerRows [data-field-remove]').forEach(e=>e.onclick=()=>{FIELDS.stack=FIELDS.stack.filter(s=>s.id!==e.dataset.fieldRemove);FIELDS.probeEpoch++;$('#fieldProbe')?.replaceChildren();stackChanged();});
  $$('#fieldLayerRows [data-field-up]').forEach(e=>e.onclick=()=>{const i=FIELDS.stack.findIndex(s=>s.id===e.dataset.fieldUp);[FIELDS.stack[i],FIELDS.stack[i+1]]=[FIELDS.stack[i+1],FIELDS.stack[i]];stackChanged();});
  $$('#fieldLayerRows [data-field-opacity]').forEach(e=>{
    e.oninput=()=>{const s=FIELDS.stack.find(s=>s.id===e.dataset.fieldOpacity);s.opacity=Number(e.value);document.getElementById('field-map-'+s.id)?.setAttribute('opacity',s.opacity);};e.onchange=()=>saveFieldStack().catch(err=>toast(err.message));
  });
}
function stackChanged(){FIELDS.probeEpoch++;$('#fieldProbe')?.replaceChildren();drawFieldStack();drawFieldCache();saveFieldStack().catch(e=>toast(e.message));renderFieldMaps();}
function fieldMapGroup(){let g=$('#reanalysisLayers');if(!g){g=svgEl('g',{id:'reanalysisLayers'});$('#coast').before(g);}return g;}
async function renderFieldMaps(){
  const epoch=++FIELDS.epoch,g=fieldMapGroup();g.replaceChildren();
  if(!S.map)return;
  if(!S.product&&FIELDS.stack.some(s=>s.visible)){const times=[...new Set(FIELDS.stack.filter(s=>s.visible).map(s=>fieldMeta(s.id)?.time).filter(Boolean))];$('#mapTitle').textContent='Поля реанализа';$('#productTag').textContent=times.length===1?times[0].replace('T',' ').replace('Z',' UTC'):'Несколько сроков · время указано у каждого слоя';}
  const context=S.product?{product:S.product.id}:{preset:S.map.grid.preset,width:S.map.grid.width};
  for(const layer of FIELDS.stack){
    if(!layer.visible)continue;
    try{
      const r=await api('/api/reanalysis/render',{id:layer.id,...context});if(epoch!==FIELDS.epoch)return;
      const group=svgEl('g',{id:'field-map-'+layer.id,opacity:layer.opacity,'pointer-events':'none'});
      if(layer.style.includes('fill'))group.append(svgEl('image',{href:r.image,x:0,y:0,width:r.grid.width,height:r.grid.height}));
      if(layer.style.includes('contours'))for(const [i,c] of r.contours.entries()){
        group.append(svgEl('polyline',{points:c.points.map(p=>p.join(',')).join(' '),class:'field-contour-halo'}),svgEl('polyline',{points:c.points.map(p=>p.join(',')).join(' '),class:'field-contour'}));
        if(i%3===0&&c.points.length>30){const p=c.points[Math.floor(c.points.length/2)],t=svgEl('text',{x:p[0],y:p[1],class:'field-contour-label'});t.textContent=num(c.value);group.append(t);}
      }
      if(layer.style.includes('arrows'))for(const v of r.vectors){
        const x2=v.x+v.dx,y2=v.y+v.dy,ux=v.dx/16,uy=v.dy/16;
        const p=`M${v.x},${v.y} L${x2},${y2} M${x2-ux*5+uy*3},${y2-uy*5-ux*3} L${x2},${y2} L${x2-ux*5-uy*3},${y2-uy*5+ux*3}`;
        group.append(svgEl('path',{d:p,class:'field-contour-halo'}),svgEl('path',{d:p,class:'field-arrow'}));
      }
      g.append(group);
      const row=$(`#fieldLayerRows [data-layer="${layer.id}"]`);
      if(row){row.querySelector('.field-render-error').textContent=r.valid_pixels?'':'В выбранной области карты нет данных.';
        const legend=row.querySelector('.field-legend');legend.innerHTML=`<div class="field-colorbar"></div><div class="field-legend-numbers"><span>${num(r.legend.min)}</span><span>${escape(r.legend.unit)}</span><span>${num(r.legend.max)}</span></div>`;legend.querySelector('.field-colorbar').style.background=`linear-gradient(to right,${r.legend.colors.join(',')})`;legend.title=r.legend.sampling;}
    }catch(e){if(epoch!==FIELDS.epoch)return;const row=$(`#fieldLayerRows [data-layer="${layer.id}"]`);if(row)row.querySelector('.field-render-error').textContent=e.message;}
  }
}
async function probeFields(x,y){
  const ids=FIELDS.stack.filter(s=>s.visible).map(s=>s.id);if(!ids.length)return;
  const epoch=++FIELDS.probeEpoch,product=S.product?.id;
  const xy=await api('/api/coordinates',{x,y,...(product?{product}:{preset:S.map.grid.preset,width:S.map.grid.width})});
  const r=await api('/api/reanalysis/probe',{...xy,ids});if(epoch!==FIELDS.probeEpoch)return;
  let box=$('#fieldProbe');if(!box){box=document.createElement('section');box.id='fieldProbe';$('#tab-point').append(box);}
  box.innerHTML=`<h3>Поля в точке</h3><p class="micro">${num(xy.lat,4)}°, ${num(xy.lon,4)}° · ближайшие исходные узлы</p>`+r.rows.map(f=>`<article class="field-probe-row"><strong>${escape(f.source_label||f.source)} · ${escape(f.title)}</strong><b>${fieldNumber(f.value)} ${escape(f.unit)}</b><span>${f.level===null?'Поверхность':num(f.level)+' гПа'} · ${escape(f.time.replace('T',' ').replace('Z',' UTC'))}</span>${f.value===null?'<p>Нет данных в исходной маске.</p>':f.direction_from_deg!==undefined?`<p>Ветер от ${num(f.direction_from_deg,0)}°</p>`:''}</article>`).join('');
  if(!S.product){$('#pixel').replaceChildren();$('#pointControls').hidden=true;}tab('point');
}
const baseFieldMap=setMap;
setMap=async function(...args){const r=await baseFieldMap(...args);if(r){drawFieldStack();renderFieldMaps();}return r;};
const baseFieldProduct=showProduct;
showProduct=async function(...args){await baseFieldProduct(...args);drawFieldStack();renderFieldMaps();};
const baseFieldClick=mapClick;
mapClick=async function(e){const p=pointerPoint(e);if(S.drawing)return baseFieldClick(e);if(S.product&&!S.product.display_only)await baseFieldClick(e);if(S.map&&p.x>=0&&p.y>=0&&p.x<S.map.grid.width&&p.y<S.map.grid.height)await probeFields(p.x,p.y);};
const baseFieldInvalidate=invalidatePoint;
invalidatePoint=function(){FIELDS.probeEpoch++;$('#fieldProbe')?.replaceChildren();return baseFieldInvalidate();};
function initFieldUI(){
  if(!$('#taskGrid button')){setTimeout(initFieldUI,100);return;}
  const block=document.createElement('section');block.id='fieldLayerPanel';block.innerHTML='<div class="section-heading"><h3>Поля реанализа</h3><button id="fieldAddOpen" class="tonal" aria-label="Добавить поле реанализа">＋</button></div><div id="fieldLayerRows"></div><p id="fieldLegacyNotice" class="notice" hidden></p><p id="fieldStatus" class="micro" role="status"></p><button id="fieldCancel" class="text-button" hidden>Остановить получение поля</button><hr>';
  $('#productPanel').prepend(block);$('#productRail span').textContent='Слои';$('#productRail').setAttribute('aria-label','Слои и продукты');
  $('#fieldAddOpen').onclick=()=>openFieldDialog().catch(e=>toast(e.message));
  $('#fieldCancel').onclick=()=>api('/api/reanalysis/cancel',{id:FIELDS.job.id}).catch(e=>toast(e.message));
  refreshFields().then(data=>{FIELDS.stack=data.stack.filter(s=>fieldMeta(s.id));drawFieldStack();renderFieldMaps();if(FIELDS.job.status==='running')pollFields();}).catch(e=>toast(e.message));
}
const baseFieldSaveAccess=saveSourceAccess;
saveSourceAccess=async function(...args){await baseFieldSaveAccess(...args);if(FIELDS.accessReturn){const source=FIELDS.accessReturn;FIELDS.accessReturn=null;await openFieldDialog(source);}};
$('#fieldConfigure').onclick=()=>{FIELDS.accessReturn=$('#fieldSource').value;fieldDialog.close();openSourceAccess(FIELDS.accessReturn).catch(e=>toast(e.message));};
$('#fieldClose').onclick=()=>fieldDialog.close();$('#fieldSource').onchange=fieldSelectors;$('#fieldVariable').onchange=fieldLevels;
$('#fieldDownload').onclick=()=>startField(false);$('#fieldImport').onclick=()=>startField(true);
$('#fieldUseRegion').onclick=()=>{const box=({arctic:[90,-180,40,180],geographic:[90,-180,40,180],barents:[85,-25,60,70],kara:[85,45,65,145]})[$('#preset').value];if(box)['#fieldNorth','#fieldWest','#fieldSouth','#fieldEast'].forEach((id,i)=>$(id).value=box[i]);};
$('#fieldDifference').onclick=async()=>{fieldError();try{const r=await api('/api/reanalysis/difference',{first:$('#fieldFirst').value,second:$('#fieldSecond').value});FIELDS.pending=r.id;FIELDS.job={id:r.id,status:'running',message:'Разность на более грубой исходной сетке'};fieldsJob();await pollFields();}catch(e){fieldError(e.message);}};
document.addEventListener('click',e=>{const button=e.target.closest('[data-field-source]');if(button)openFieldDialog(button.dataset.fieldSource).catch(err=>toast(err.message));});
initFieldUI();
