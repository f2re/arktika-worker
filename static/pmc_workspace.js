/* Рабочая наноска ПМЦ: снимок расчёта, экспертные пометки, PNG и закладки.
 * Научный результат неизменяем: экспертная оценка хранится отдельно.
 */
'use strict';
const PMCW={result:null,annotations:[],annotationGrid:null,selected:null,drawing:false,
  restored:false,bookmarkId:null,dirty:false,formDirty:false,revision:0,workspaceEpoch:0,restoring:false,fieldRender:Promise.resolve(),warnings:[],unavailableFields:[]};
const PMC_MARK_STATUS={manual_candidate:'Ручной кандидат',expert_supported:'Поддержан экспертом',rejected:'Отклонён экспертом'};
const PMC_MARK_COLORS={manual_candidate:'#fbbf24',expert_supported:'#22c55e',rejected:'#ef4444'};
const pmcCopy=value=>value===undefined?null:JSON.parse(JSON.stringify(value));

function pmcMapIdentity(g){return g?JSON.stringify([g.preset,g.crs,g.width,g.height,g.bounds]):'';}
function pmcAnnotationMapMatches(){return !PMCW.annotations.length||pmcMapIdentity(PMCW.annotationGrid)===pmcMapIdentity(S.map?.grid);}
function pmcWorkspaceMessage(message,error=false){
  const box=$('#pmcWorkspaceStatus');box.textContent=message;box.classList.toggle('error-text',error);
}
function pmcWorkspaceDirty(){PMCW.dirty=true;PMCW.revision++;pmcWorkspaceMessage('Есть несохранённые изменения.');}
function pmcResultKey(result){return result?JSON.stringify([result.field_id,result.source,result.valid_time,result.algorithm_version,result.parameters,result.grid]):'';}

function pmcInitWorkspace(){
  const panel=document.createElement('section');panel.id='pmcWorkspacePanel';panel.hidden=true;
  panel.innerHTML=`<p class="hint">1. Подготовьте поля и рассчитайте кандидаты. 2. Сопоставьте их с облачностью и полями среды. 3. Сохраните наноску и закладку.</p>
    <button id="pmcPanelAnalysis" class="primary full">Поля и расчёт кандидатов</button>
    <p class="notice">Вихревой кандидат — повод для проверки. Экспертная пометка сохраняет вашу оценку и не меняет результат детектора.</p>
    <div id="pmcCurrentAnalysis"></div><button id="pmcReturnMap" class="text-button" hidden>Вернуть карту анализа</button>
    <details id="pmcReviewFields"><summary>Поля среды этого срока</summary><button id="pmcContextMap" class="tonal full">Нанести ветер 850 гПа и давление</button><div id="pmcContextFields"></div><p class="micro">Кнопка выбирает только тот же источник и точный срок UTC. Маски и температура показаны для экспертной проверки; критерии ПМЦ автоматически не подтверждаются.</p></details>
    <details open><summary>Кандидаты на карте</summary><div id="pmcCandidateList"></div></details>
    <details open><summary>Ручная наноска · <span id="pmcAnnotationCount">0</span></summary>
      <p class="micro">«Нанести центр» — щелчок по карте. Кандидата из списка можно взять за основу пометки. Перетаскивание сдвигает карту.</p>
      <div id="pmcAnnotationList"></div>
      <form id="pmcAnnotationForm" hidden><h3 id="pmcAnnotationHeading">Пометка</h3>
        <label>Подпись<input id="pmcAnnotationLabel" maxlength="120" required></label>
        <label>Оценка<select id="pmcAnnotationStatus"><option value="manual_candidate">Ручной кандидат</option><option value="expert_supported">Поддержан экспертом</option><option value="rejected">Отклонён экспертом</option></select></label>
        <label>Основание и наблюдения<textarea id="pmcAnnotationNote" rows="3" maxlength="2000" placeholder="Морская область, облачная спираль, ветер, неустойчивость; источник и срок подтверждающих данных"></textarea></label>
        <p class="micro">Для поддержки или отклонения кандидата укажите основание. Это запись экспертного решения, без оценки его точности.</p>
        <div class="row"><button type="submit" class="tonal">Применить пометку</button><button id="pmcAnnotationDelete" type="button" class="text-button">Удалить</button></div><p id="pmcAnnotationError" role="alert" class="error-text"></p>
      </form>
    </details>
    <hr><label>Название анализа<input id="pmcWorkspaceTitle" maxlength="160" placeholder="Регион и случай ПМЦ"></label>
    <label>Общие заметки<textarea id="pmcWorkspaceNotes" rows="3" maxlength="10000" placeholder="Что проверено и что требует дальнейшего анализа"></textarea></label>
    <div class="row"><button id="pmcBookmarkSave" class="primary">Сохранить закладку</button><button id="pmcBookmarkSaveAs" class="text-button">Сохранить копию</button></div>
    <p class="micro">Закладка сохраняет срок, проекцию, масштаб, слои, пороги, снимок расчёта и пометки. Для повторной отрисовки полей нужен локальный кэш.</p>`;
  $('#leftPanel').append(panel);
  const dialog=document.createElement('dialog');dialog.id='pmcBookmarksDialog';dialog.className='wide';
  dialog.innerHTML='<div class="dialog-head"><h2>Закладки анализа ПМЦ</h2><button id="pmcBookmarksClose" aria-label="Закрыть">✕</button></div><p class="micro">Открывается сохранённый анализ. Повторный расчёт запускается отдельно по исходным полям.</p><div id="pmcBookmarkList" role="status"></div>';
  document.body.append(dialog);
  bind('#pmcWorkspaceOpen',()=>left('pmc',false));
  bind('#pmcAnalysisOpen',openPmcArchive);bind('#pmcPanelAnalysis',openPmcArchive);
  bind('#pmcDrawToggle',()=>pmcSetDrawing(!PMCW.drawing));
  bind('#pmcPngExport',pmcExportPng);bind('#pmcQuickBookmark',()=>{left('pmc');$('#pmcWorkspaceTitle').focus();});
  bind('#pmcBookmarkSave',()=>pmcSaveBookmark(false));bind('#pmcBookmarkSaveAs',()=>pmcSaveBookmark(true));
  bind('#pmcBookmarksOpen',pmcOpenBookmarks);bind('#pmcBookmarksClose',()=>dialog.close());
  bind('#pmcContextMap',pmcShowContextFields);
  bind('#pmcReturnMap',async()=>{const g=PMCW.annotationGrid;if(g){clearMapProduct();await setMap(g.preset,g.width);$('#preset').value=g.preset;pmcWorkspaceDraw();}});
  bind('#pmcAnnotationDelete',()=>{PMCW.annotations=PMCW.annotations.filter(a=>a.id!==PMCW.selected);PMCW.selected=null;pmcWorkspaceDirty();pmcWorkspaceRender();});
  $('#pmcAnnotationForm').addEventListener('submit',event=>{event.preventDefault();pmcApplyAnnotation();});
  for(const id of ['#pmcWorkspaceTitle','#pmcWorkspaceNotes'])$(id).addEventListener('input',pmcWorkspaceDirty);
  for(const id of ['#pmcAnnotationLabel','#pmcAnnotationNote','#pmcAnnotationStatus'])$(id).addEventListener('input',()=>{PMCW.formDirty=true;pmcWorkspaceDirty();});
  $('#pmcArchiveDialog').addEventListener('close',()=>{if(PMCW.result||PMCW.annotations.length)pmcWorkspaceDraw();});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&PMCW.drawing){pmcSetDrawing(false);event.preventDefault();}});
  pmcWorkspaceRender();
}

const pmcBaseLeft=left;
left=function(mode='catalog',force=true){
  pmcBaseLeft(mode,force);
  const visible=!$('#leftPanel').hidden;
  $('#pmcWorkspacePanel').hidden=mode!=='pmc';
  $('#pmcWorkspaceOpen').classList.toggle('active',visible&&mode==='pmc');
  $('#pmcWorkspaceOpen').setAttribute('aria-expanded',String(visible&&mode==='pmc'));
  if(mode==='pmc'){$('#leftHeading').textContent='Анализ ПМЦ';$('#pmcWorkspaceBar').hidden=false;}
};

function pmcSetDrawing(active){
  if(active&&PMCW.restoring)throw Error('Дождитесь открытия закладки.');
  if(active&&!S.map)throw Error('Дождитесь загрузки карты.');
  if(active&&!pmcAnnotationMapMatches())throw Error('Верните карту анализа перед добавлением пометок.');
  PMCW.drawing=Boolean(active);
  $('#pmcDrawToggle').setAttribute('aria-pressed',String(PMCW.drawing));
  $('#pmcDrawToggle').textContent=PMCW.drawing?'Завершить наноску':'Нанести центр';
  $('#map').classList.toggle('pmc-drawing',PMCW.drawing);
  if(active){S.drawing=false;left('pmc');if(innerWidth<1050)$('#leftPanel').hidden=true;pmcWorkspaceMessage('Щёлкните по центру вихря. Esc завершает наноску.');}
}

const pmcBaseMapClick=mapClick;
mapClick=async function(event){
  if(!PMCW.drawing)return pmcBaseMapClick(event);
  const point=pointerPoint(event),g=S.map?.grid;
  if(!g||point.x<0||point.y<0||point.x>=g.width||point.y>=g.height)return;
  await pmcAddAnnotation(point.x,point.y);
};

async function pmcAddAnnotation(x,y,candidate=null){
  pmcFlushAnnotation();
  if(PMCW.annotations.length>=250)throw Error('В одном анализе допускается до 250 пометок.');
  if(!pmcAnnotationMapMatches())throw Error('Верните карту анализа перед нанесением.');
  const grid=pmcCopy(S.map.grid),identity=pmcMapIdentity(grid),analysisKey=pmcResultKey(PMCW.result);
  const position=await api('/api/coordinates',{x,y,...(S.product?{product:S.product.id}:{preset:grid.preset,width:grid.width})});
  if(identity!==pmcMapIdentity(S.map?.grid)||analysisKey!==pmcResultKey(PMCW.result))throw Error('Карта или анализ изменились во время определения координат. Повторите наноску.');
  const item={id:'an-'+crypto.randomUUID(),x,y,lon:position.lon,lat:position.lat,
    label:candidate?.candidate_id||`Пометка ${PMCW.annotations.length+1}`,note:'',status:'manual_candidate',
    candidate_id:candidate?.candidate_id||null,valid_time:PMCW.result?.valid_time||S.product?.time||null,source:PMCW.result?.source||null};
  if(!PMCW.annotations.length)PMCW.annotationGrid=grid;
  PMCW.annotations.push(item);PMCW.selected=item.id;pmcSetDrawing(false);pmcWorkspaceDirty();left('pmc');pmcWorkspaceRender();
  $('#pmcAnnotationLabel').focus();
}

function pmcSelectAnnotation(id){if(!pmcApplyAnnotation())return;PMCW.selected=id;left('pmc');pmcWorkspaceRender();}
function pmcApplyAnnotation(){
  const item=PMCW.annotations.find(a=>a.id===PMCW.selected);if(!item)return true;
  const label=$('#pmcAnnotationLabel').value.trim(),note=$('#pmcAnnotationNote').value.trim(),status=$('#pmcAnnotationStatus').value;
  const error=$('#pmcAnnotationError');error.textContent='';
  if(!label){error.textContent='Введите подпись.';return false;}
  if(status!=='manual_candidate'&&!note){error.textContent='Укажите основание экспертного решения.';return false;}
  PMCW.formDirty=false;
  if(item.label!==label||item.note!==note||item.status!==status){Object.assign(item,{label,note,status});pmcWorkspaceDirty();pmcWorkspaceRender();}
  return true;
}
function pmcFlushAnnotation(){if(!pmcApplyAnnotation())throw Error($('#pmcAnnotationError').textContent);}

function pmcCandidateMapPoint(result,candidate){
  const g=S.map?.grid,rg=result?.grid,rc=candidate?.peak_pixel_rc;
  if(!g||!rg||g.crs!==rg.crs||!Array.isArray(rc)||rc.length!==2||!rc.every(Number.isFinite))return null;
  const [a,b,c,d,e,f]=rg.transform||[],[row,col]=rc,[left,bottom,right,top]=g.bounds;
  const x=(c+a*(col+.5)+b*(row+.5)-left)*g.width/(right-left);
  const y=(top-f-d*(col+.5)-e*(row+.5))*g.height/(top-bottom);
  return Number.isFinite(x+y)&&x>=0&&y>=0&&x<g.width&&y<g.height?{x,y}:null;
}

function pmcMatchingContext(){
  const r=PMCW.result;if(!r)return [];
  return FIELDS.fields.filter(f=>!PMCW.unavailableFields.includes(f.id)&&!f.difference_of&&f.source===r.source&&Date.parse(f.time)===Date.parse(r.valid_time));
}
async function pmcShowContextFields(){
  // SVG paints later layers above earlier ones: keep pressure over any wind fill.
  const fields=pmcMatchingContext().filter(f=>f.variable==='mslp'||(f.variable==='wind'&&Number(f.level)===850))
    .sort((a,b)=>Number(a.variable==='mslp')-Number(b.variable==='mslp'));
  if(!fields.length)throw Error('Для этого источника и срока нет полей в локальном кэше. Подготовьте их через «Поля и расчёт».');
  FIELDS.stack=fields.slice(0,8).map(f=>({id:f.id,opacity:1,visible:true,style:f.variable==='mslp'?'contours':'arrows'}));
  await saveFieldStack();drawFieldStack();drawFieldCache();await renderFieldMaps();pmcWorkspaceDirty();pmcWorkspaceMessage('Показаны доступные ветер 850 гПа и давление этого срока.');
}

async function pmcPresentResult(result,{restored=false}={}){
  if(PMCW.restoring)throw Error('Дождитесь открытия закладки.');
  if(!restored&&PMCW.dirty&&PMCW.annotations.length&&pmcResultKey(result)!==pmcResultKey(PMCW.result)){
    if(!await ask('Открыть другой расчёт?','Несохранённые пометки текущего анализа будут заменены. Отмените действие, чтобы сначала сохранить закладку.'))return false;
  }
  const crs=result.grid?.crs,preset=Object.keys(S.registry?.presets||{}).find(key=>S.registry.presets[key].crs===crs);
  if(S.map?.grid.crs!==crs){
    if(!preset)throw Error('Для сетки результата нет поддерживаемой карты: '+String(crs));
    clearMapProduct();await setMap(preset,Math.max(256,Math.min(2048,Number(result.grid.width)||1000)));$('#preset').value=preset;
  }
  const changed=pmcResultKey(result)!==pmcResultKey(PMCW.result);
  if(changed&&!restored){PMCW.annotations=[];PMCW.annotationGrid=null;PMCW.selected=null;PMCW.bookmarkId=null;PMCW.warnings=[];PMCW.unavailableFields=[];PMCW.workspaceEpoch++;}
  PMCW.result=pmcCopy(result);PMCW.restored=restored;PMCW.dirty=!restored;PMCW.revision++;
  pmcRenderRunResult(result);pmcWorkspaceRender();$('#pmcWorkspaceBar').hidden=false;
  if(!$('#pmcWorkspaceTitle').value||changed)$('#pmcWorkspaceTitle').value=`ПМЦ · ${PMC_SOURCE_LABELS[result.source]||result.source||'реанализ'} · ${pmcTime(result.valid_time)}`;
  pmcWorkspaceMessage(restored?'Сохранённый анализ открыт; расчёт не повторялся.':'Кандидаты нанесены. Проверьте поля среды и облачность.');
  return true;
}

function pmcWorkspaceRender(){
  const summary=$('#pmcCurrentAnalysis');if(!summary)return;summary.replaceChildren();
  if(PMCW.result){
    const r=PMCW.result;pmcText(summary,'h3',`${PMC_SOURCE_LABELS[r.source]||r.source||'Источник'} · ${r.candidates?.length||0} кандидатов`);
    pmcText(summary,'p',pmcTime(r.valid_time),'micro');
    pmcText(summary,'p',PMCW.restored?'Сохранённый результат · не пересчитан':'Расчёт по реанализу · экспериментальные кандидаты','pmc-result-state');
    pmcRenderThresholds(r.parameters,summary);
    const limits=document.createElement('details');pmcText(limits,'summary','Что ещё необходимо проверить');
    pmcText(limits,'p','Морская поверхность и лёд, давление и приземный ветер, неустойчивость между поверхностью моря и 500 гПа, размер и эволюция вихря, облачная структура. По одной завихрённости ПМЦ не подтверждается.','micro');summary.append(limits);
  }else pmcText(summary,'p','Расчёт ещё не открыт. Можно нанести ручную пометку на карте или загрузить поля.','empty');
  for(const warning of PMCW.warnings)pmcText(summary,'p',warning,'notice');
  const otherTimes=FIELDS.stack.filter(layer=>layer.visible&&PMCW.result&&Date.parse(fieldMeta(layer.id)?.time)!==Date.parse(PMCW.result.valid_time));
  if(otherTimes.length)pmcText(summary,'p',`На карте есть поля других сроков: ${otherTimes.length}. Сверьте время слоёв перед интерпретацией.`,'notice');
  if(S.product&&PMCW.result&&Date.parse(S.product.time)!==Date.parse(PMCW.result.valid_time))pmcText(summary,'p','Спутниковый снимок и расчёт имеют разные сроки. Сверьте время перед сравнением.','notice');
  $('#pmcReturnMap').hidden=pmcAnnotationMapMatches();
  const context=$('#pmcContextFields');context.replaceChildren();$('#pmcContextMap').disabled=!PMCW.result;
  const matching=pmcMatchingContext();
  for(const field of matching){const button=pmcText(context,'button',`${field.title||field.variable}${field.level?' · '+field.level+' гПа':''}`,'text-button');button.type='button';button.onclick=()=>addCachedField(field.id).then(()=>{pmcWorkspaceDirty();pmcWorkspaceMessage('Поле добавлено на карту: '+(field.title||field.variable));}).catch(e=>pmcWorkspaceMessage(e.message,true));}
  if(!matching.length)pmcText(context,'p','Поля текущего источника и срока в кэше не найдены.','micro');
  const candidates=$('#pmcCandidateList');candidates.replaceChildren();
  for(const candidate of PMCW.result?.candidates||[]){
    const card=document.createElement('article');card.className='pmc-review-card';
    pmcText(card,'strong',candidate.candidate_id||'Кандидат');
    const peak=candidate['peak_850hpa_relative_vorticity_s-1'];
    pmcText(card,'p',`ζ850: ${Number.isFinite(peak)?peak.toExponential(3):'—'} с⁻¹ · ячеек ${num(candidate.pixel_count,0)}`,'micro');
    const quality=candidate.spatial_quality||{};
    if(quality.boundary_censored)pmcText(card,'p','Область касается границы данных; размер не завершён.','field-time-warning');
    if(quality.complete_smoothing_support===false)pmcText(card,'p','Неполная окрестность сглаживания.','field-time-warning');
    if(candidate.geometry?.status==='omitted_complexity_limit')pmcText(card,'p','Контур слишком сложен; показан только пик.','micro');
    const action=pmcText(card,'button','Оценить на карте','text-button');action.type='button';
    action.onclick=async()=>{try{const point=pmcCandidateMapPoint(PMCW.result,candidate);if(!point)throw Error('Кандидат вне текущей карты. Откройте результат в соответствующей проекции.');
      const existing=PMCW.annotations.find(a=>a.candidate_id===candidate.candidate_id);
      if(existing)pmcSelectAnnotation(existing.id);else await pmcAddAnnotation(point.x,point.y,candidate);
    }catch(e){pmcWorkspaceMessage(e.message,true);}};
    candidates.append(card);
  }
  if(!candidates.childElementCount)pmcText(candidates,'p',PMCW.result?'В расчёте кандидатов нет. Это не означает отсутствие ПМЦ.':'Кандидаты появятся после расчёта.','micro');
  $('#pmcAnnotationCount').textContent=String(PMCW.annotations.length);
  const list=$('#pmcAnnotationList');list.replaceChildren();
  for(const item of PMCW.annotations){
    const button=pmcText(list,'button',`${item.label} · ${PMC_MARK_STATUS[item.status]}`,'pmc-annotation-select');
    button.type='button';button.classList.toggle('selected',item.id===PMCW.selected);button.onclick=()=>pmcSelectAnnotation(item.id);
  }
  const selected=PMCW.annotations.find(a=>a.id===PMCW.selected),form=$('#pmcAnnotationForm');form.hidden=!selected;
  if(selected){$('#pmcAnnotationHeading').textContent=`${num(selected.lat,3)}°, ${num(selected.lon,3)}° · ${pmcTime(selected.valid_time)}`;
    if(!PMCW.formDirty||form.dataset.annotationId!==selected.id){$('#pmcAnnotationLabel').value=selected.label;$('#pmcAnnotationNote').value=selected.note;$('#pmcAnnotationStatus').value=selected.status;$('#pmcAnnotationError').textContent='';PMCW.formDirty=false;}form.dataset.annotationId=selected.id;}
  else{form.dataset.annotationId='';PMCW.formDirty=false;}
  pmcWorkspaceDraw();
}

function pmcWorkspaceDraw(){
  if(PMCW.restoring)return;
  if(PMCW.result)pmcDrawOverlay(PMCW.result);
  let layer=$('#pmcAnnotations');if(!layer){layer=svgEl('g',{id:'pmcAnnotations','aria-label':'Ручные пометки метеоролога'});$('#mapContent').append(layer);}layer.replaceChildren();
  if(!pmcAnnotationMapMatches())return;
  for(const item of PMCW.annotations){
    const group=svgEl('g',{'data-annotation':item.id,role:'button',tabindex:0,'aria-label':`${item.label}: ${PMC_MARK_STATUS[item.status]}`});
    const color=PMC_MARK_COLORS[item.status];
    group.append(svgEl('circle',{cx:item.x,cy:item.y,r:item.id===PMCW.selected?11:8,fill:'#0f172a','fill-opacity':'.8',stroke:color,'stroke-width':2.5,'vector-effect':'non-scaling-stroke'}));
    group.append(svgEl('path',{d:`M${item.x-4},${item.y}h8M${item.x},${item.y-4}v8`,stroke:color,'stroke-width':1.5,'vector-effect':'non-scaling-stroke'}));
    const text=svgEl('text',{x:item.x+13,y:item.y-11,fill:color,'font-size':13,'font-family':'sans-serif',stroke:'#0f172a','stroke-width':3,'paint-order':'stroke'});text.textContent=item.label;group.append(text);
    const title=svgEl('title');title.textContent=`${PMC_MARK_STATUS[item.status]} · ${item.note||'Основание не записано'}`;group.append(title);
    group.addEventListener('pointerdown',event=>{event.stopPropagation();});
    group.addEventListener('click',event=>{event.stopPropagation();pmcSelectAnnotation(item.id);});
    group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();pmcSelectAnnotation(item.id);}});
    layer.append(group);
  }
}

const pmcBaseSetMap=setMap;
setMap=async function(...args){const identity=pmcMapIdentity(S.map?.grid);const result=await pmcBaseSetMap(...args);if(result){if(identity!==pmcMapIdentity(result.grid))PMCW.workspaceEpoch++;pmcClearOverlay();pmcSetDrawing(false);pmcWorkspaceRender();}return result;};
const pmcBaseRenderFields=renderFieldMaps;
renderFieldMaps=function(...args){const pending=pmcBaseRenderFields(...args);PMCW.fieldRender=pending;return pending;};

function pmcWorkspaceSnapshot(){
  pmcFlushAnnotation();
  if(!S.map)throw Error('Карта ещё не загружена.');
  if(!pmcAnnotationMapMatches())throw Error('Верните карту анализа перед сохранением пометок.');
  const g=S.map.grid;
  let run=null,archive=null;
  if($('#pmcCachedField'))run={field_id:$('#pmcCachedField').value||null,preset:$('#pmcRunCrs').value,width:Number($('#pmcRunWidth').value),observation_id:$('#pmcObservation').value||null,thresholds:pmcReadThresholds()||null};
  try{archive={...pmcArchiveBody(),include_context:$('#pmcIncludeContext').checked};}catch{}
  return pmcCopy({schema_version:'arktika.pmc_workspace.v1',map:{preset:g.preset,width:g.width,height:g.height,crs:g.crs,bounds:g.bounds,view:S.view},
    product_id:S.product?.id||null,field_stack:FIELDS.stack,raster_visible:$('#layerToggle').checked,night_visible:$('#nightToggle').checked,
    result:PMCW.result,run,archive,annotations:PMCW.annotations});
}

async function pmcSaveBookmark(asCopy=false){
  if(PMCW.restoring)throw Error('Дождитесь открытия закладки.');
  const title=$('#pmcWorkspaceTitle').value.trim();if(!title){left('pmc');$('#pmcWorkspaceTitle').focus();throw Error('Введите название закладки.');}
  const button=$('#pmcBookmarkSave');button.disabled=true;$('#pmcBookmarkSaveAs').disabled=true;
  try{const body={title,notes:$('#pmcWorkspaceNotes').value,state:pmcWorkspaceSnapshot()};if(PMCW.bookmarkId&&!asCopy)body.id=PMCW.bookmarkId;
    const revision=PMCW.revision,epoch=PMCW.workspaceEpoch,bookmarkId=PMCW.bookmarkId;
    const display=JSON.stringify([S.view,S.map?.grid,FIELDS.stack,$('#layerToggle').checked,$('#nightToggle').checked]);
    const result=await api('/api/pmc/bookmarks/save',body);if(!result.bookmark?.id)throw Error('Сервер не подтвердил сохранение закладки.');
    if(epoch===PMCW.workspaceEpoch&&bookmarkId===PMCW.bookmarkId)PMCW.bookmarkId=result.bookmark.id;
    if(revision===PMCW.revision&&epoch===PMCW.workspaceEpoch&&display===JSON.stringify([S.view,S.map?.grid,FIELDS.stack,$('#layerToggle').checked,$('#nightToggle').checked])){PMCW.dirty=false;pmcWorkspaceMessage('Закладка сохранена: '+result.bookmark.title);toast('Анализ и пометки сохранены.');}
    else if(epoch===PMCW.workspaceEpoch){PMCW.dirty=true;pmcWorkspaceMessage('Снимок закладки сохранён; более поздние изменения ещё не сохранены.');}
    else toast('Снимок предыдущего анализа сохранён; текущая закладка не изменена.');
  }finally{button.disabled=false;$('#pmcBookmarkSaveAs').disabled=false;}
}

async function pmcOpenBookmarks(){
  const dialog=$('#pmcBookmarksDialog'),root=$('#pmcBookmarkList');root.textContent='Загружаю закладки…';if(!dialog.open)dialog.showModal();
  try{const response=await api('/api/pmc/bookmarks');root.replaceChildren();
    for(const bookmark of response.bookmarks||[]){
      const card=document.createElement('article');card.className='pmc-bookmark-card';pmcText(card,'h3',bookmark.title);
      pmcText(card,'p',`Сохранено ${pmcTime(bookmark.updated_at||bookmark.created_at)}`,'micro');if(bookmark.notes)pmcText(card,'p',bookmark.notes);
      const row=document.createElement('div');row.className='row';card.append(row);
      const show=pmcText(row,'button','Открыть анализ','tonal');show.type='button';show.onclick=async()=>{show.disabled=true;try{if(await pmcRestoreBookmark(bookmark.id)!==false)dialog.close();}catch(e){pmcText(card,'p',e.message,'error-text');}finally{show.disabled=false;}};
      const remove=pmcText(row,'button','Удалить закладку','text-button');remove.type='button';remove.onclick=async()=>{if(!await ask('Удалить закладку?',bookmark.title))return;remove.disabled=true;try{await api('/api/pmc/bookmarks/delete',{id:bookmark.id});if(PMCW.bookmarkId===bookmark.id)PMCW.bookmarkId=null;card.remove();if(!root.childElementCount)root.textContent='Сохранённых закладок пока нет.';}catch(e){remove.disabled=false;pmcText(card,'p',e.message,'error-text');}};
      root.append(card);
    }
    if(!root.childElementCount)root.textContent='Сохранённых закладок пока нет. Откройте анализ, добавьте название и сохраните его.';
  }catch(e){root.textContent='Не удалось загрузить закладки: '+e.message;}
}

async function pmcRestoreBookmark(id){
  if(PMCW.restoring)throw Error('Другая закладка уже открывается. Дождитесь завершения.');
  PMCW.restoring=true;
  let previous=null,changed=false,completed=false;
  const lock=active=>{for(const selector of ['#shell','#pmcBookmarkList'])if($(selector))$(selector).inert=active;};
  try{
    const response=await api('/api/pmc/bookmark?id='+encodeURIComponent(id)),bookmark=response.bookmark,state=bookmark?.state;
    if(!state||state.schema_version!=='arktika.pmc_workspace.v1')throw Error('Неизвестный формат закладки.');
    if(PMCW.dirty&&!await ask('Открыть закладку?','Несохранённые изменения текущего анализа будут заменены.'))return false;
    const oldWork={};for(const key of ['result','annotations','annotationGrid','selected','restored','bookmarkId','dirty','formDirty','revision','workspaceEpoch','warnings','unavailableFields'])oldWork[key]=pmcCopy(PMCW[key]);
    previous={work:oldWork,grid:pmcCopy(S.map?.grid),view:pmcCopy(S.view),product:pmcCopy(S.product),stack:pmcCopy(FIELDS.stack),
      title:$('#pmcWorkspaceTitle').value,notes:$('#pmcWorkspaceNotes').value,raster:$('#layerToggle').checked,night:$('#nightToggle').checked,
      draft:{label:$('#pmcAnnotationLabel').value,note:$('#pmcAnnotationNote').value,status:$('#pmcAnnotationStatus').value}};
    lock(true);pmcSetDrawing(false);pmcWorkspaceMessage('Открываю сохранённый анализ…');
    // Resolve all remote dependencies and verify the grid before replacing any
    // current analysis. Missing cached fields are reported, never substituted.
    const [cache,observations,products]=await Promise.all([api('/api/reanalysis/state'),api('/api/pmc/observations'),state.product_id?api('/api/products'):Promise.resolve({products:[]})]);
    const availability=response.availability||bookmark.availability||{},warnings=[...(availability.warnings||[])];
    const product=(products.products||[]).find(p=>p.id===state.product_id)||null;
    if(state.product_id&&!product)warnings.push('Спутниковый продукт отсутствует; восстановлена географическая основа.');
    const preview=await api('/api/map?'+qs(product?{product:product.id}:{preset:state.map.preset,width:state.map.width}));
    if(pmcMapIdentity(preview.grid)!==pmcMapIdentity(state.map))throw Error('Сетка сохранённой карты изменилась. Пометки не перенесены на другую сетку.');
    const unavailable=new Set([...(availability.missing_fields||[]),...(availability.changed_fields||[])]);
    const known=new Set((cache.fields||[]).filter(f=>!unavailable.has(f.id)).map(f=>f.id)),missing=(state.field_stack||[]).filter(layer=>!known.has(layer.id));
    const stack=(state.field_stack||[]).filter(layer=>known.has(layer.id)).map(layer=>({...layer}));
    if(missing.length)warnings.push(`Нет локальных полей: ${missing.length}. Они не подменены другими сроками.`);
    changed=true;clearMapProduct();
    if(product)await showProduct(product);else await setMap(state.map.preset,state.map.width);
    const g=S.map.grid;
    if(pmcMapIdentity(g)!==pmcMapIdentity(state.map))throw Error('Карта изменилась во время открытия закладки.');
    $('#preset').value=g.preset;S.view=[...state.map.view];$('#map').setAttribute('viewBox',S.view.join(' '));
    FIELDS.fields=cache.fields||[];FIELDS.stack=stack;
    await saveFieldStack();drawFieldStack();drawFieldCache();await renderFieldMaps();
    $('#layerToggle').checked=state.raster_visible;$('#raster').setAttribute('visibility',state.raster_visible?'visible':'hidden');
    $('#nightToggle').checked=state.night_visible;$('#nightRaster').setAttribute('visibility',state.night_visible?'visible':'hidden');
    pmcRunPanel();pmcCachedChoices(FIELDS.fields.filter(f=>!unavailable.has(f.id)));pmcObservationChoices(observations.observations||[]);
    if(state.run){const run=state.run;$('#pmcRunCrs').value=run.preset;$('#pmcRunWidth').value=String(run.width);$('#pmcCachedField').value=run.field_id||'';$('#pmcObservation').value=run.observation_id||'';
      $('#pmcMinimumThreshold').value=run.thresholds?String(run.thresholds['minimum_vorticity_s-1']*1e4):'1';$('#pmcPeakThreshold').value=run.thresholds?String(run.thresholds['peak_vorticity_s-1']*1e4):'1.2';$('#pmcRunButton').disabled=!$('#pmcCachedField').value;}
    pmcInvalidateArchivePlan();pmcRenderSources(cache.catalogue?.sources||[]);
    if(state.archive){const a=state.archive;$('#pmcArchiveStart').value=a.start;$('#pmcArchiveEnd').value=a.end;['#pmcNorth','#pmcWest','#pmcSouth','#pmcEast'].forEach((selector,index)=>$(selector).value=a.area[index]);$$('[name="pmcArchiveSource"]').forEach(input=>input.checked=a.sources.includes(input.value));$('#pmcIncludeContext').checked=a.include_context;}
    PMCW.result=pmcCopy(state.result);PMCW.annotations=pmcCopy(state.annotations||[]);PMCW.annotationGrid=pmcCopy(g);PMCW.selected=null;PMCW.formDirty=false;PMCW.restored=true;PMCW.bookmarkId=bookmark.id;PMCW.warnings=warnings;PMCW.unavailableFields=[...unavailable];PMCW.dirty=false;PMCW.revision++;PMCW.workspaceEpoch++;
    $('#pmcWorkspaceTitle').value=bookmark.title;$('#pmcWorkspaceNotes').value=bookmark.notes||'';
    completed=true;
    const limited=unavailable.size>0||missing.length>0||availability.product_available===false;
    pmcWorkspaceMessage(limited?'Сохранённый анализ открыт с ограничениями; см. панель.':'Сохранённый анализ открыт; расчёт не повторялся.');
    return true;
  }catch(error){
    if(changed&&previous){
      Object.assign(PMCW,previous.work);FIELDS.stack=previous.stack;
      $('#pmcWorkspaceTitle').value=previous.title;$('#pmcWorkspaceNotes').value=previous.notes;
      try{clearMapProduct();if(previous.product)await showProduct(previous.product);else if(previous.grid)await setMap(previous.grid.preset,previous.grid.width);
        if(previous.view){S.view=previous.view;$('#map').setAttribute('viewBox',S.view.join(' '));}
        if(previous.grid)$('#preset').value=previous.grid.preset;
        await saveFieldStack();drawFieldStack();drawFieldCache();await renderFieldMaps();
        $('#layerToggle').checked=previous.raster;$('#raster').setAttribute('visibility',previous.raster?'visible':'hidden');$('#nightToggle').checked=previous.night;$('#nightRaster').setAttribute('visibility',previous.night?'visible':'hidden');
      }catch{PMCW.warnings=[...previous.work.warnings,'Карта недоступна после сбоя открытия. Предыдущий расчёт и пометки сохранены в текущей сессии; повторите открытие после восстановления связи.'];}
    }
    pmcWorkspaceMessage('Закладка не открыта; текущие пометки сохранены. '+error.message,true);throw error;
  }finally{
    PMCW.restoring=false;lock(false);
    if(completed||changed){if(PMCW.result)pmcRenderRunResult(PMCW.result);pmcWorkspaceRender();}
    if(completed)left('pmc');
    else if(previous&&changed){$('#pmcAnnotationLabel').value=previous.draft.label;$('#pmcAnnotationNote').value=previous.draft.note;$('#pmcAnnotationStatus').value=previous.draft.status;}
  }
}

async function pmcWaitForMap(){
  for(let attempt=0;attempt<4;attempt++){const pending=PMCW.fieldRender;await pending;if(pending===PMCW.fieldRender)return;}
  throw Error('Карта ещё обновляется. Повторите сохранение после загрузки слоёв.');
}
function pmcBlobDataUrl(blob){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(Error('Не удалось прочитать слой изображения.'));reader.readAsDataURL(blob);});}

async function pmcExportPng(){
  if(PMCW.restoring)throw Error('Дождитесь открытия закладки.');
  pmcFlushAnnotation();
  if(!S.map)throw Error('Карта ещё не загружена.');
  if(!pmcAnnotationMapMatches())throw Error('Верните карту анализа перед экспортом наноски.');
  const button=$('#pmcPngExport');button.disabled=true;pmcWorkspaceMessage('Готовлю карту со всеми видимыми слоями…');
  let objectUrl;
  try{
    await pmcWaitForMap();
    const failed=$$('#fieldLayerRows .field-render-error').filter(e=>e.textContent&&FIELDS.stack.find(layer=>layer.id===e.closest('[data-layer]')?.dataset.layer)?.visible);
    if(failed.length)throw Error('Не все видимые поля построены: '+failed.map(e=>e.textContent).join('; '));
    // Freeze provenance in the same synchronous turn as the SVG clone. Fetching
    // raster bytes can take time; later UI changes must not relabel this image.
    const snapshot=pmcCopy({result:PMCW.result,annotations:PMCW.annotations,warnings:PMCW.warnings,restored:PMCW.restored,
      title:$('#pmcWorkspaceTitle').value,view:S.view,grid:S.map.grid,product:S.product,raster_visible:$('#layerToggle').checked,
      legends:FIELDS.stack.filter(layer=>layer.visible).map(layer=>({field:fieldMeta(layer.id),style:layer.style,render:FIELDS.rendered?.[layer.id]}))});
    const original=$('#map'),svg=original.cloneNode(true),elements=[original,...original.querySelectorAll('*')],copies=[svg,...svg.querySelectorAll('*')];
    const properties=['fill','fill-opacity','stroke','stroke-width','stroke-opacity','stroke-dasharray','stroke-linecap','stroke-linejoin','opacity','visibility','display','font-family','font-size','font-weight','text-anchor','paint-order','vector-effect'];
    for(let i=0;i<elements.length;i++){const style=getComputedStyle(elements[i]);for(const property of properties)copies[i].style.setProperty(property,style.getPropertyValue(property));}
    for(const image of svg.querySelectorAll('image')){
      const href=image.getAttribute('href');if(!href){image.remove();continue;}
      if(image.style.visibility==='hidden'||image.style.display==='none'){image.remove();continue;}
      const url=new URL(href,location.href);
      if(url.origin!==location.origin||!/^\/(?:artifact|field-render)\//.test(url.pathname))throw Error('Изображение карты не является локальным слоем.');
      const response=await fetch(url.href);if(!response.ok)throw Error('Недоступен слой карты: '+url.pathname);
      const blob=await response.blob();if(!blob.type.startsWith('image/'))throw Error('Вместо слоя карты получен другой тип данных.');
      image.setAttribute('href',await pmcBlobDataUrl(blob));
    }
    const view=snapshot.view||[0,0,snapshot.grid.width,snapshot.grid.height],mapWidth=1600,mapHeight=Math.round(mapWidth*view[3]/view[2]);
    if(mapHeight>3200)throw Error('Область карты слишком вытянута для PNG. Измените масштаб.');
    svg.setAttribute('width',String(mapWidth));svg.setAttribute('height',String(mapHeight));svg.setAttribute('viewBox',view.join(' '));
    svg.style.width=mapWidth+'px';svg.style.height=mapHeight+'px';svg.style.maxHeight='none';
    objectUrl=URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(svg)],{type:'image/svg+xml;charset=utf-8'}));
    const image=new Image();image.src=objectUrl;await image.decode();
    const lines=[];const r=snapshot.result;
    if(r){lines.push(`${PMC_SOURCE_LABELS[r.source]||r.source} · ${pmcTime(r.valid_time)} · ${r.candidate_count??r.candidates?.length??0} кандидатов`);
      lines.push(`Алгоритм ${r.algorithm_id||'не указан'} ${r.algorithm_version||''} · ${snapshot.grid.crs}`);
      if(r.parameters)lines.push(`Пороги ζ850: область ${r.parameters['minimum_vorticity_s-1']} с⁻¹; пик ${r.parameters['peak_vorticity_s-1']} с⁻¹`);
    }else lines.push(`${snapshot.grid.name||snapshot.grid.preset} · ${snapshot.grid.crs} · ручная наноска без открытого расчёта`);
    const legends=snapshot.legends;
    if(snapshot.product&&snapshot.raster_visible){const p=snapshot.product;lines.push(`Спутник: ${p.title} · ${p.platform} · ${pmcTime(p.time)}`);
      if(p.legend?.palette){const l=p.legend,celsius=l.quantity_kind==='absolute_temperature'&&l.units==='K',offset=celsius?273.15:0;
        legends.unshift({field:{source_label:p.platform,title:p.title,time:p.time},style:'fill',render:{legend:{colors:l.palette.map(color=>'rgb('+color.join(',')+')'),min:l.display_min-offset,max:l.display_max-offset,unit:celsius?'°C':l.units}}});}
    }
    const footerLines=[snapshot.restored?'Сохранённый результат; повторный расчёт не выполнялся.':'Экспериментальные кандидаты по реанализу; не подтверждённые ПМЦ.',
      'Фиолетовый: модельный кандидат; жёлтый: ручной; зелёный: поддержан экспертом; красный: отклонён экспертом.',
      'Контур — граница модельной области выше порога, не физическая граница циклона. Отсутствие кандидатов не означает отсутствие ПМЦ.',
      ...(snapshot.product&&snapshot.raster_visible?[snapshot.product.legend?.meaning||'',...(snapshot.product.legend?.components||[]).map(c=>`Компонента ${c.component}: ${c.formula} · ${c.min}…${c.max} ${c.units} · γ ${c.gamma}`)]:[]),
      ...snapshot.annotations.map(a=>`${a.label} · ${PMC_MARK_STATUS[a.status]} · ${num(a.lat,3)}°, ${num(a.lon,3)}° · ${a.note||'Основание не записано'}`),
      ...snapshot.warnings,`Экспорт UTC: ${new Date().toISOString()} · подложка Natural Earth`];
    const canvas=document.createElement('canvas'),ctx=canvas.getContext('2d');canvas.width=mapWidth;ctx.font='20px sans-serif';
    const wrap=text=>{const output=[];let line='';for(const word of String(text).split(/\s+/)){const next=line?line+' '+word:word;if(line&&ctx.measureText(next).width>mapWidth-64){output.push(line);line=word;}else line=next;}if(line)output.push(line);return output;};
    const heading=lines.flatMap(wrap),foot=footerLines.flatMap(wrap);const headerHeight=80+heading.length*28,legendHeight=legends.length*66;
    canvas.height=headerHeight+mapHeight+legendHeight+foot.length*28+38;if(canvas.height>16000)throw Error('Слишком много текста для одного PNG. Сократите заметки или сохраните несколько анализов.');
    ctx.fillStyle='#f8fafc';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.fillStyle='#0f172a';ctx.font='bold 28px sans-serif';ctx.fillText(snapshot.title||'Анализ ПМЦ',32,43,mapWidth-64);
    ctx.font='20px sans-serif';heading.forEach((line,index)=>ctx.fillText(line,32,78+index*28));ctx.drawImage(image,0,headerHeight,mapWidth,mapHeight);
    let y=headerHeight+mapHeight+27;
    for(const {field,style,render} of legends){const legend=fieldLegendPresentation(style,render);
      ctx.fillStyle='#0f172a';ctx.fillText(`${field?.source_label||field?.source} · ${field?.title||field?.variable} ${field?.level?field.level+' гПа':''} · ${pmcTime(field?.time)}`,32,y);y+=20;
      if(legend.colors.length){const gradient=ctx.createLinearGradient(32,0,432,0);legend.colors.forEach((color,index)=>gradient.addColorStop(index/Math.max(1,legend.colors.length-1),color));ctx.fillStyle=gradient;ctx.fillRect(32,y,400,14);ctx.fillStyle='#0f172a';ctx.fillText(`${num(legend.min,2)} … ${num(legend.max,2)} ${legend.unit}${legend.description?' · '+legend.description:''}`,450,y+14,mapWidth-482);}
      else if(legend.symbol){ctx.strokeStyle='#193a61';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(32,y+7);ctx.lineTo(92,y+7);if(legend.symbol==='arrow'){ctx.lineTo(82,y+2);ctx.moveTo(92,y+7);ctx.lineTo(82,y+12);}ctx.stroke();ctx.fillText(legend.description,110,y+14,mapWidth-142);}
      else ctx.fillText('Шкала недоступна',32,y+14);y+=46;
    }
    ctx.fillStyle='#334155';foot.forEach((line,index)=>ctx.fillText(line,32,y+index*28));
    const blob=await new Promise((resolve,reject)=>canvas.toBlob(value=>value?resolve(value):reject(Error('PNG не сформирован.')),'image/png'));
    const download=URL.createObjectURL(blob),link=document.createElement('a');link.href=download;link.download=`pmc-analysis-${String(r?.valid_time||new Date().toISOString()).replace(/[^0-9TZ]/g,'-')}.png`;link.click();setTimeout(()=>URL.revokeObjectURL(download),10000);
    pmcWorkspaceMessage('PNG подготовлен: карта, поля, легенды и наноска.');
  }catch(e){pmcWorkspaceMessage('Не удалось сохранить PNG: '+e.message,true);throw e;}
  finally{if(objectUrl)URL.revokeObjectURL(objectUrl);button.disabled=false;}
}

pmcInitWorkspace();
