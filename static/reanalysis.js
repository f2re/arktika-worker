/* Пространственные поля: независимый от спутника стек, точные сроки, без CDN. */
'use strict';
const FIELDS={catalogue:[],fields:[],stack:[],epoch:0,probeEpoch:0,pending:null,timer:null,saveTail:Promise.resolve(),job:{status:'idle'},credentialRetry:null,accessReturn:null,accessSaving:false,credentialPromptOpen:false};
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
</div><div class="dialog-footer"><a href="/docs/REANALYSIS.html" target="_blank" rel="noopener">Поля и ограничения</a><button id="fieldDialogCancel" class="text-button" hidden>Отменить сохранённый запрос</button><button id="fieldDownload" class="primary">Загрузить и добавить на карту</button></div>`;
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
function credentialDialog(action){
  const spec=action.credential_request;
  if(!spec||spec.provider!==action.provider||spec.format!=='fields'||!Array.isArray(spec.fields)||!spec.fields.length)
    throw Error('Адаптер передал неполное описание реквизитов доступа.');
  if(FIELDS.credentialPromptOpen&&document.querySelector('dialog[data-credential-provider]'))return;
  FIELDS.credentialPromptOpen=true;
  const dialog=document.createElement('dialog');dialog.className='wide fixed-dialog';
  dialog.dataset.credentialProvider=spec.provider;
  const head=document.createElement('div');head.className='dialog-head';
  const title=document.createElement('h2');title.textContent=`Доступ: ${action.provider}`;
  const close=document.createElement('button');close.type='button';close.textContent='✕';close.setAttribute('aria-label','Закрыть');
  head.append(title,close);dialog.append(head);
  if(spec.help){const help=document.createElement('p');help.className='hint';help.textContent=spec.help;dialog.append(help);}
  const form=document.createElement('form');form.method='dialog';
  const values={};
  for(const field of spec.fields){
    if(!field||typeof field.id!=='string'||!/^[a-z][a-z0-9_]{0,39}$/.test(field.id)||
       !['text','password'].includes(field.type)||typeof field.label!=='string'||
       !Number.isInteger(field.max_length)||field.max_length<1||field.max_length>8192||typeof field.required!=='boolean')
      throw Error('Адаптер передал небезопасную схему ввода реквизитов.');
    const label=document.createElement('label');label.textContent=field.label;
    const input=document.createElement('input');input.type=field.type;input.autocomplete='off';input.maxLength=field.max_length;input.required=field.required;
    label.append(input);form.append(label);values[field.id]=input;
    if(field.help){const hint=document.createElement('p');hint.className='micro';hint.textContent=field.help;form.append(hint);}
  }
  const error=document.createElement('p');error.className='error-text';error.setAttribute('role','alert');error.hidden=true;form.append(error);
  const save=document.createElement('button');save.type='submit';save.className='primary';save.textContent='Сохранить в памяти и продолжить';form.append(save);
  const clear=document.createElement('button');clear.type='button';clear.className='text-button';clear.textContent='Удалить сохранённый доступ';form.append(clear);
  const cancel=document.createElement('button');cancel.type='button';cancel.className='text-button';cancel.textContent='Отмена';form.append(cancel);
  dialog.append(form);document.body.append(dialog);
  const cleanup=()=>{if(dialog.open)dialog.close();dialog.remove();};
  dialog.addEventListener('close',()=>{dialog.remove();FIELDS.credentialPromptOpen=false;});
  close.onclick=cancel.onclick=cleanup;
  clear.onclick=async()=>{error.hidden=true;clear.disabled=true;try{await api('/api/sources/credentials',{provider:spec.provider,clear:true});error.textContent='Сохранённый доступ удалён из памяти процесса.';error.hidden=false;}catch(e){error.textContent=e.message;error.hidden=false;}finally{clear.disabled=false;}};
  form.onsubmit=async event=>{
    event.preventDefault();error.hidden=true;save.disabled=true;
    try{
      const payload={};for(const [id,input] of Object.entries(values))payload[id]=input.value;
      await api('/api/sources/credentials',{provider:spec.provider,format:spec.format,values:payload});
      const result=await api('/api/reanalysis/resume',{id:action.id});
      FIELDS.job={...FIELDS.job,...result,id:action.id,status:result.started?'running':'needs_action',message:'Продолжается сохранённый запрос'};
      FIELDS.pending=action.id;cleanup();await openFieldDialog(action.source);fieldsJob();await pollFields();
    }catch(e){error.textContent=e.message;error.hidden=false;save.disabled=false;}
  };
  dialog.showModal();
}
function fieldsJob(){
  const j=FIELDS.job,busy=j.status==='running',pendingAction=j.status==='needs_action'||(j.status==='interrupted'&&j.resume_available);
  $('#fieldJobMessage').textContent=busy?(j.message||''):j.status==='needs_action'?'Запрос сохранён. Настройте доступ к источнику; после сохранения запрос продолжится с теми же параметрами.':(j.status==='interrupted'&&j.resume_available?'Запрос прерван остановкой сервера. Его можно продолжить после настройки доступа.':'');
  if($('#fieldStatus'))$('#fieldStatus').textContent=j.message||'';
  if($('#fieldCancel')){$('#fieldCancel').hidden=!(busy||j.status==='needs_action'||(j.status==='interrupted'&&j.resume_available));$('#fieldCancel').disabled=false;}
  if($('#fieldDialogCancel'))$('#fieldDialogCancel').hidden=!(busy||j.status==='needs_action'||(j.status==='interrupted'&&j.resume_available));
  if($('#fieldRetry'))$('#fieldRetry').hidden=!(j.status==='error'&&j.retry_available===true&&j.resume_available===true);
  for(const id of ['#fieldDownload','#fieldImport','#fieldDifference'])$(id).disabled=busy||pendingAction;
  $('#fieldConfigure').disabled=busy;
  if(j.status==='error'||(j.status==='interrupted'&&!j.resume_available))fieldError(j.message);
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
  const action=FIELDS.job.needs_action;
  if(!FIELDS.credentialPromptOpen&&FIELDS.job.status==='needs_action'&&action?.code==='credentials_required'&&
     action.provider!=='cds'&&action.provider!=='earthdata'&&action.credential_request){
    FIELDS.credentialPromptOpen=true;credentialDialog(action);
  }
}
function drawFieldCache(){
  $('#fieldCache').innerHTML=FIELDS.fields.length?FIELDS.fields.map(f=>`<article class="field-cache-row"><span>${escape(fieldLabel(f))}</span><button class="tonal" data-field-add="${f.id}" ${FIELDS.stack.some(s=>s.id===f.id)?'disabled':''}>Добавить</button></article>`).join(''):'<p class="hint">Пока нет сохранённых полей.</p>';
  $$('#fieldCache [data-field-add]').forEach(b=>b.onclick=()=>addCachedField(b.dataset.fieldAdd).catch(e=>fieldError(e.message)));
  const originals=FIELDS.fields.filter(f=>!f.difference_of);
  for(const selector of ['#fieldFirst','#fieldSecond']){const old=$(selector).value;$(selector).innerHTML=originals.map(f=>`<option value="${f.id}">${escape(fieldLabel(f))}</option>`).join('');if(originals.some(f=>f.id===old))$(selector).value=old;}
  if($('#fieldFirst').value===$('#fieldSecond').value&&originals.length>1)$('#fieldSecond').selectedIndex=1;
}
async function startField(local=false,frozenBody=null){
  fieldError();try{
    const body=frozenBody?{...frozenBody,area:[...frozenBody.area]}:fieldBody();if(local)body.path=$('#fieldPath').value;
    const result=await api('/api/reanalysis/'+(local?'import':'add'),body);
    if(result.needs_action?.code==='credentials_required'){
      const action=result.needs_action;
      if(!/^[a-z][a-z0-9_-]{1,63}$/.test(action.provider)||typeof action.source!=='string'||typeof action.id!=='string')throw Error('Источник вернул неизвестный запрос доступа.');
      FIELDS.credentialRetry={id:action.id,source:action.source,provider:action.provider,body:{...body,area:[...body.area]}};
      FIELDS.job={id:action.id,status:'needs_action',resume_available:true,needs_action:{source:action.source,provider:action.provider}};fieldsJob();
      fieldDialog.close();
      if(['cds','earthdata'].includes(action.provider))await openSourceAccess(action.source);
      else credentialDialog(action);
      return;
    }
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
  const block=document.createElement('section');block.id='fieldLayerPanel';block.innerHTML='<div class="section-heading"><h3>Поля реанализа</h3><button id="fieldAddOpen" class="tonal" aria-label="Добавить поле реанализа">＋</button></div><div id="fieldLayerRows"></div><p id="fieldLegacyNotice" class="notice" hidden></p><p id="fieldStatus" class="micro" role="status"></p><button id="fieldRetry" class="text-button" hidden>Повторить запрос</button><button id="fieldCancel" class="text-button" hidden>Отменить сохранённый запрос</button><hr>';
  $('#productPanel').prepend(block);$('#productRail span').textContent='Слои';$('#productRail').setAttribute('aria-label','Слои и продукты');
  $('#fieldAddOpen').onclick=()=>{FIELDS.credentialPromptOpen=false;openFieldDialog().catch(e=>toast(e.message));};
const cancelFieldJob=async()=>{try{await api('/api/reanalysis/cancel',{id:FIELDS.job.id});await refreshFields();}catch(e){toast(e.message);}};
$('#fieldCancel').onclick=cancelFieldJob;$('#fieldDialogCancel').onclick=cancelFieldJob;
$('#fieldRetry').onclick=async()=>{try{const result=await api('/api/reanalysis/resume',{id:FIELDS.job.id});FIELDS.job={...FIELDS.job,...result,status:'running',message:'Повторяется сохранённый запрос'};FIELDS.pending=FIELDS.job.id;fieldsJob();await pollFields();}catch(e){fieldError(e.message);}};
  refreshFields().then(data=>{FIELDS.stack=data.stack.filter(s=>fieldMeta(s.id));drawFieldStack();renderFieldMaps();if(FIELDS.job.status==='running')pollFields();}).catch(e=>toast(e.message));
}
const baseFieldSaveAccess=saveSourceAccess;
saveSourceAccess=async function(...args){FIELDS.accessSaving=true;try{await baseFieldSaveAccess(...args);}catch(e){FIELDS.accessSaving=false;throw e;}FIELDS.accessSaving=false;const retry=FIELDS.credentialRetry;const source=retry?.source||FIELDS.accessReturn;FIELDS.credentialRetry=null;FIELDS.accessReturn=null;if(source){await openFieldDialog(source);const job=FIELDS.job,action=job.needs_action,jobSource=action?.source||job.pending?.spec?.source;if(job.id&&job.resume_available&&jobSource===source){const result=await api('/api/reanalysis/resume',{id:job.id});if(result.started){FIELDS.pending=job.id;FIELDS.job={...job,status:'running',message:'Продолжается сохранённый запрос'};fieldDialog.close();fieldsJob();await pollFields();}else{FIELDS.job={...job,...result};fieldsJob();}}else if(retry)await startField(false,retry.body);}};
for(const selector of ['#cdsAccessDialog','#earthdataAccessDialog'])$(selector).addEventListener('close',()=>{if(!FIELDS.accessSaving){const source=FIELDS.credentialRetry?.source||FIELDS.accessReturn;FIELDS.credentialRetry=null;FIELDS.accessReturn=null;if(source)openFieldDialog(source).catch(e=>toast(e.message));}});
$('#fieldConfigure').onclick=()=>{FIELDS.accessReturn=$('#fieldSource').value;fieldDialog.close();openSourceAccess(FIELDS.accessReturn).catch(e=>toast(e.message));};
$('#fieldClose').onclick=()=>fieldDialog.close();$('#fieldSource').onchange=fieldSelectors;$('#fieldVariable').onchange=fieldLevels;
$('#fieldDownload').onclick=()=>startField(false);$('#fieldImport').onclick=()=>startField(true);
$('#fieldUseRegion').onclick=()=>{const box=({arctic:[90,-180,40,180],geographic:[90,-180,40,180],barents:[85,-25,60,70],kara:[85,45,65,145]})[$('#preset').value];if(box)['#fieldNorth','#fieldWest','#fieldSouth','#fieldEast'].forEach((id,i)=>$(id).value=box[i]);};
$('#fieldDifference').onclick=async()=>{fieldError();try{const r=await api('/api/reanalysis/difference',{first:$('#fieldFirst').value,second:$('#fieldSecond').value});FIELDS.pending=r.id;FIELDS.job={id:r.id,status:'running',message:'Разность на более грубой исходной сетке'};fieldsJob();await pollFields();}catch(e){fieldError(e.message);}};
document.addEventListener('click',e=>{const button=e.target.closest('[data-field-source]');if(button)openFieldDialog(button.dataset.fieldSource).catch(err=>toast(err.message));});
initFieldUI();
