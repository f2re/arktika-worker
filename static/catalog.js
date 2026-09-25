/* Каталог: состав каналов, поиск и выбор набора. Запросы GPTL — только по кнопке. */
'use strict';
const CATALOG = {rows:[], coverage:'unknown', error:'', key:'', visible:24, fingerprint:'',
  bundles:[], pending:null, downloading:new Set(), filesSequence:0, fileSession:null};
const catalogStates={local:'На диске',remote:'В каталоге',queued:'В очереди',running:'Загружается',
  paused:'Пауза',error:'Ошибка загрузки',unregistered:'Файл не распознан как канал',absent:'Не найден в загруженном каталоге'};
const coverageNames={complete:'Каталог проверен за весь день',current:'Проверена доступная часть текущего дня',
  partial:'Каталог получен частично',error:'Запрос каталога завершился ошибкой',unknown:'Полнота каталога не проверена',
  current_unchecked:'Текущий день: полнота каталога не проверена',future:'Будущая дата: наблюдений ещё нет'};
const sessionKey=s=>s.platform+'|'+s.time;
const catalogContext=()=>[S.day,$('#platform').value,$('#catalogTask').value,$('#catalogChannel').value].join('|');

function cancelCatalogOpen(){CATALOG.pending=null;}
function catalogPlan(row,task=$('#catalogTask').value,channel=Number($('#catalogChannel').value)){
  const bundle=CATALOG.bundles.find(b=>b.id===task);
  if(!bundle)throw Error('Неизвестный набор каналов.');
  const required=bundle.channels||[channel],inventory=row.channel_inventory||[];
  const selected=required.map(ch=>inventory.find(c=>c.channel===ch)||{channel:ch,state:'absent'});
  const downloadable=selected.filter(c=>c.candidate_id);
  return {required,local:selected.filter(c=>c.state==='local').map(c=>c.channel),
    waiting:selected.filter(c=>['queued','running'].includes(c.state)).map(c=>c.channel),
    absent:selected.filter(c=>c.state==='absent').map(c=>c.channel),
    blocked:selected.filter(c=>c.state==='unregistered').map(c=>c.channel),
    failed:selected.filter(c=>['error','paused'].includes(c.state)).map(c=>c.channel),
    ids:downloadable.map(c=>c.candidate_id),size:downloadable.reduce((n,c)=>n+(c.size||0),0),
    unknown:downloadable.filter(c=>c.size==null).length,ready:required.every(ch=>inventory.some(c=>c.channel===ch&&c.state==='local')),
    canOpen:!['ir','all'].includes(task)};
}
function catalogSize(plan){return plan.unknown?(plan.size?size(plan.size)+' + неизвестный размер':'размер не указан'):size(plan.size);}
function catalogPlanText(plan){
  const pieces=['На диске '+plan.local.length+' из '+plan.required.length];
  if(plan.waiting.length)pieces.push('в очереди: '+plan.waiting.join(', '));
  if(plan.absent.length)pieces.push('не найдены: '+plan.absent.join(', '));
  if(plan.blocked.length)pieces.push('не распознаны: '+plan.blocked.join(', '));
  if(plan.failed.length)pieces.push('нужен повтор: '+plan.failed.join(', '));
  return pieces.join(' · ');
}
function catalogMatches(row,query,availability){
  const plan=catalogPlan(row),channels=new Set([...(row.local_channels||[]),...(row.catalog_channels||[])]);
  const names=S.registry.channels.filter(c=>channels.has(c.id)).map(c=>c.id+' '+c.name+' '+c.description).join(' ');
  const haystack=[row.platform,row.platform==='ARCM1'?'Арктика-М1':'Арктика-М2',row.time,row.search_text,
    ...(row.levels||[]),names].join(' ').toLocaleLowerCase('ru');
  if(!query.toLocaleLowerCase('ru').trim().split(/\s+/).every(w=>haystack.includes(w)))return false;
  if(availability==='ready')return plan.ready;
  if(availability==='download')return plan.ids.length>0;
  if(availability==='problem')return plan.failed.length>0||plan.blocked.length>0||Boolean(row.no_uri&&!row.local_id);
  return true;
}
function renderCatalog(){
  if(!$('#catalogSearch'))return;
  const rows=CATALOG.rows.filter(r=>catalogMatches(r,$('#catalogSearch').value,$('#catalogAvailability').value));
  const signature=JSON.stringify([rows,CATALOG.visible,S.scene?.id,$('#catalogTask').value,$('#catalogChannel').value,CATALOG.error,CATALOG.coverage]);
  if(signature===CATALOG.fingerprint)return;
  CATALOG.fingerprint=signature;
  $('#catalogStatus').textContent=CATALOG.error||coverageNames[CATALOG.coverage]||coverageNames.unknown;
  $('#catalogStatus').classList.toggle('error-text',Boolean(CATALOG.error)||CATALOG.coverage==='error');
  $('#sessionCount').textContent=rows.length===CATALOG.rows.length?String(rows.length):rows.length+' / '+CATALOG.rows.length;
  $('#catalogChannelLabel').hidden=$('#catalogTask').value!=='channel';
  $('#catalogMore').hidden=rows.length<=CATALOG.visible;
  if(!rows.length){
    const filtered=CATALOG.rows.length>0;
    $('#sessions').innerHTML='<p class="empty">'+(filtered?'По этим условиям ничего не найдено. Сбросьте фильтр или измените поиск.':CATALOG.coverage==='future'?'Выберите прошедшую дату.':CATALOG.coverage==='complete'?'Запрос завершён: доступных файлов за день нет. Можно открыть локальные GeoTIFF.':'Нет локальных снимков и записей каталога. Нажмите «Найти снимки» или откройте папку GeoTIFF.')+'</p>';
    return;
  }
  $('#sessions').innerHTML=rows.slice(0,CATALOG.visible).map(row=>{
    const plan=catalogPlan(row),key=sessionKey(row),busy=CATALOG.downloading.has(key);
    const title=row.platform==='ARCM1'?'Арктика-М1':'Арктика-М2';
    const action=plan.ready?'Открыть':plan.ids.length?(plan.canOpen&&!plan.absent.length&&!plan.blocked.length?'Скачать и открыть':'Скачать доступные'):plan.waiting.length?'Загружается':'Нет набора';
    const channels=(row.channel_inventory||[]).map(c=>`<span class="catalog-channel ${escape(c.state)}" title="Канал ${c.channel}: ${escape(catalogStates[c.state]||c.state)}" aria-label="Канал ${c.channel}: ${escape(catalogStates[c.state]||c.state)}">${c.channel}</span>`).join('');
    return `<article class="catalog-card ${row.local_id===S.scene?.id?'selected':''}" data-session="${escape(key)}"><header><strong><time>${escape(row.time.slice(11,19))}</time> UTC</strong><span>${title}</span></header><div class="catalog-channels" aria-label="Состав каналов">${channels}</div><p class="catalog-completeness">${escape(catalogPlanText(plan))}</p>${row.no_uri&&!row.local_id?'<p class="error-text">Есть запись, но нет ссылок на файлы.</p>':''}<div class="catalog-actions"><button class="session primary" data-key="${escape(key)}" ${busy||(!plan.ready&&!plan.ids.length)?'disabled':''}>${busy?'Добавление…':action}</button><button class="catalog-files text-button" data-key="${escape(key)}">${row.count?'Файлы · '+row.count:row.local_channels.length?'Каналы · '+row.local_channels.length:'Запись'}</button></div>${plan.ids.length?`<small>К загрузке: ${plan.ids.length} · ${escape(catalogSize(plan))}</small>`:''}</article>`;
  }).join('');
  $$('#sessions button.session').forEach(b=>b.onclick=()=>{
    const row=CATALOG.rows.find(r=>sessionKey(r)===b.dataset.key);if(!row)return;
    if(catalogPlan(row).ready)openCatalogScene(row);else queueCatalog(row).catch(e=>catalogError(e.message));
  });
  $$('#sessions .catalog-files').forEach(b=>b.onclick=()=>{
    const row=CATALOG.rows.find(r=>sessionKey(r)===b.dataset.key);if(row)showFiles(row).catch(e=>catalogError(e.message));
  });
}
function catalogError(message){CATALOG.error=message;CATALOG.fingerprint='';renderCatalog();}
function openCatalogScene(row,task=$('#catalogTask').value,channel=Number($('#catalogChannel').value)){
  const scene=S.scenes.find(s=>s.id===row.local_id);
  if(!scene){catalogError('Локальный снимок изменился. Обновите каталог.');return;}
  cancelCatalogOpen();
  if(typeof clearRange==='function')clearRange();
  $('#product').value=['ir','all'].includes(task)?'channel':task;
  $('#channel').value=task==='channel'?channel:scene.channels.some(c=>c.channel===9)?9:scene.channels[0].channel;
  selectScene(scene);productHint();requestBuild();
  if(innerWidth<1050)$('#leftPanel').hidden=true;
}
async function queueCatalog(row){
  const key=sessionKey(row);if(CATALOG.downloading.has(key))return;
  const context=catalogContext(),task=$('#catalogTask').value,channel=Number($('#catalogChannel').value);
  CATALOG.downloading.add(key);CATALOG.fingerprint='';renderCatalog();
  try{
    const fresh=await api('/api/session?'+qs({platform:row.platform,time:row.time}));
    if(context!==catalogContext())return;
    const plan=catalogPlan(fresh,task,channel);
    if(plan.ready){await loadDay();if(context===catalogContext())openCatalogScene({...row,...fresh},task,channel);return;}
    if(!plan.ids.length){await loadDay();return;}
    if(plan.size>=512*1024*1024&&!await ask('Загрузка большого набора',plan.ids.length+' файлов · '+catalogSize(plan)))return;
    if(context!==catalogContext())return;
    const result=await api('/api/queue',{ids:plan.ids});
    if(context===catalogContext()){
      CATALOG.pending=plan.canOpen&&!plan.absent.length&&!plan.blocked.length?{key,context,task,channel}:null;
      CATALOG.error='';await loadDay();
    }
    toast('В очередь: '+result.count+' файлов · '+row.time.slice(11,19)+' UTC');
  }finally{CATALOG.downloading.delete(key);CATALOG.fingerprint='';renderCatalog();}
}
async function loadDay(){
  const generation=++S.generation,day=S.day,platform=$('#platform').value,key=day+'|'+platform;
  if(key!==CATALOG.key){CATALOG.key=key;CATALOG.rows=[];CATALOG.visible=24;CATALOG.error='';CATALOG.coverage='unknown';CATALOG.fingerprint='';$('#sessions').innerHTML='<p class="hint">Чтение каталога…</p>';}
  if(CATALOG.pending&&CATALOG.pending.context!==catalogContext())cancelCatalogOpen();
  try{
    const [local,first]=await Promise.all([api('/api/scenes?'+qs({date:day,platform})),api('/api/catalog?'+qs({day,platform,category:'all',limit:60}))]);
    if(generation!==S.generation)return;
    let rows=first.sessions;
    for(let offset=60;offset<first.total;offset+=60){
      const next=await api('/api/catalog?'+qs({day,platform,category:'all',limit:60,offset}));
      if(generation!==S.generation)return;
      rows=rows.concat(next.sessions);
    }
    S.scenes=local.scenes;S.catalog=rows;CATALOG.rows=rows;CATALOG.coverage=first.coverage;CATALOG.error='';
    if(first.bundles)CATALOG.bundles=first.bundles;
    renderCatalog();if(typeof timeStrip==='function')timeStrip();
    if(CATALOG.pending){
      const pending=CATALOG.pending,row=rows.find(r=>sessionKey(r)===pending.key);
      if(pending.context!==catalogContext())cancelCatalogOpen();
      else if(row){const plan=catalogPlan(row,pending.task,pending.channel);if(plan.failed.length||plan.blocked.length)cancelCatalogOpen();else if(plan.ready&&S.scenes.some(s=>s.id===row.local_id))openCatalogScene(row,pending.task,pending.channel);}
    }else if(!S.scene&&S.scenes.length){selectScene(S.scenes[S.scenes.length-1]);}
  }catch(e){if(generation===S.generation)catalogError(e.message);}
}
async function loadCalendar(){
  const month=S.month,platform=$('#platform').value,generation=(S.calendarGeneration||0)+1;S.calendarGeneration=generation;
  const result=await api('/api/calendar?'+qs({month,platform,category:'all'}));
  if(month!==S.month||platform!==$('#platform').value||generation!==S.calendarGeneration)return;
  const first=(new Date(month+'-01T00:00:00Z').getUTCDay()+6)%7;
  $('#calendar').innerHTML='<span></span>'.repeat(first)+result.days.map(d=>`<button class="${d.date===S.day?'selected ':''}${d.local_scenes?'local ':''}${d.state==='error'?'error':''}" data-date="${d.date}" ${d.state==='future'?'disabled':''} aria-label="${escape(d.date+'; '+(coverageNames[d.state]||coverageNames.unknown)+'; файлов '+d.files+'; локальных сроков '+(d.local_scenes||0))}" title="${escape(coverageNames[d.state]||coverageNames.unknown)}">${Number(d.date.slice(8))}<span class="availability">${d.local_scenes?'●':d.files?'·':d.state==='unknown'?'?':''}</span></button>`).join('');
  $$('#calendar button').forEach(b=>b.onclick=()=>setDate(b.dataset.date));
}
async function showFiles(row){
  const sequence=++CATALOG.filesSequence;CATALOG.fileSession=null;S.fileAssets=[];S.selection.clear();
  $('#filesTitle').textContent=(row.platform==='ARCM1'?'Арктика-М1':'Арктика-М2')+' · '+row.time.replace('T',' ').replace('Z',' UTC');
  $('#fileRows').textContent='Чтение списка файлов…';$('#filesSearch').value='';$('#fileCategory').value='all';
  $('#downloadSelected').disabled=true;$('#fileSelection').textContent='';$('#filesError').hidden=true;
  if(!$('#filesDialog').open)open('#filesDialog');
  try{
    const result=await api('/api/session?'+qs({platform:row.platform,time:row.time}));
    if(sequence!==CATALOG.filesSequence||!$('#filesDialog').open)return;
    CATALOG.fileSession=result;S.fileAssets=result.assets;drawFiles();
  }catch(e){if(sequence===CATALOG.filesSequence){$('#fileRows').textContent='Список не получен.';fileError(e.message);}}
}
function fileError(message){$('#filesError').textContent=message;$('#filesError').hidden=!message;}
function drawFiles(){
  const query=$('#filesSearch').value.trim().toLocaleLowerCase('ru'),category=$('#fileCategory').value;
  const rows=S.fileAssets.filter(a=>(category==='all'||a.category===category)&&[a.filename,a.title,channelMeta(a.channel)?.name].join(' ').toLocaleLowerCase('ru').includes(query));
  const localRows=(CATALOG.fileSession?.local_files||[]).filter(c=>(category==='all'||category==='channel')&&[c.filename,channelMeta(c.channel)?.name].join(' ').toLocaleLowerCase('ru').includes(query));
  const localHTML=localRows.length?'<details class="local-file-summary" '+(!S.fileAssets.length||query?'open':'')+'><summary>На диске: '+localRows.length+' каналов</summary><table><thead><tr><th>Канал</th><th>Исходный файл</th><th>Проекция</th><th>Размер</th></tr></thead><tbody>'+localRows.map(c=>`<tr><td>${c.channel}</td><td>${escape(c.filename)}<small>${escape(channelMeta(c.channel)?.name||'')}</small></td><td>${escape(c.crs||'Не указана')}</td><td>${size(c.size)}</td></tr>`).join('')+'</tbody></table></details>':'';
  $('#fileRows').innerHTML=localHTML+(rows.length?'<table><thead><tr><th>Выбор</th><th>Содержание</th><th>Проекция</th><th>Размер</th><th>Доступ</th></tr></thead><tbody>'+rows.map(a=>{
    const ch=channelMeta(a.channel),label=a.category==='channel'&&ch?'Канал '+a.channel+' · '+ch.wavelength_um+' мкм · '+ch.name:(categoryNames[a.category]||'Другой файл');
    const active=['queued','running'].includes(a.job_state);
    return `<tr><td><input type="checkbox" data-id="${escape(a.id)}" ${S.selection.has(a.id)?'checked':''} ${active?'disabled':''} aria-label="Выбрать ${escape(label+' '+a.filename)}"></td><td><strong>${escape(label)}</strong><small>${escape(a.level||'Уровень не указан')}${a.job_state?' · '+escape({missing:'Файл удалён',done:'Скачан',queued:'В очереди',running:'Загружается',paused:'Пауза',error:'Ошибка'}[a.job_state]||a.job_state):''}</small><details><summary>Имя файла</summary>${escape(a.filename)}</details></td><td>${a.epsg?'EPSG:'+a.epsg:'Не указана'}</td><td>${size(a.size)}</td><td><button class="probe text-button" data-id="${escape(a.id)}">Проверить</button></td></tr>`;
  }).join('')+'</tbody></table>':S.fileAssets.length?'<p class="empty">В каталоге нет файлов по выбранным условиям.</p>':localRows.length?'':'<p class="empty">У записи нет ссылок на файлы.</p>');
  $$('#fileRows input').forEach(c=>c.onchange=()=>{c.checked?S.selection.add(c.dataset.id):S.selection.delete(c.dataset.id);fileSelection();});
  $$('#fileRows .probe').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const r=await api('/api/check',{id:b.dataset.id});b.textContent='Доступен · '+r.http;}catch(e){fileError(e.message);}finally{b.disabled=false;}});
  fileSelection();
}
function fileSelection(){
  const rows=S.fileAssets.filter(a=>S.selection.has(a.id)),visible=new Set($$('#fileRows input').map(e=>e.dataset.id));
  const hidden=rows.filter(a=>!visible.has(a.id)).length;
  const plan={size:rows.reduce((n,a)=>n+(a.size||0),0),unknown:rows.filter(a=>a.size==null).length};
  $('#fileSelection').textContent=rows.length?'Выбрано '+rows.length+' · '+catalogSize(plan)+(hidden?' · вне фильтра: '+hidden:''):'Выберите файлы или нажмите «Набор по задаче».';
  if(!S.fileAssets.length)$('#fileSelection').textContent=CATALOG.fileSession?.local_files?.length?'Локальные каналы уже доступны; скачивать их повторно не нужно.':'Нет файлов для скачивания.';
  $('#downloadSelected').disabled=!rows.length;
  $('#selectChannels').disabled=!CATALOG.fileSession||!catalogPlan(CATALOG.fileSession).ids.length;
  $('#clearFiles').disabled=!rows.length;
}
function initCatalog(){
  CATALOG.bundles=S.registry.products.filter(p=>['implemented','experimental'].includes(p.status)&&p.id!=='motion').map(p=>({id:p.id,title:p.title,channels:p.channels||null}));
  CATALOG.bundles.push({id:'ir',title:'Все ИК-каналы · 4–10',channels:[4,5,6,7,8,9,10]},{id:'all',title:'Все каналы · 1–10',channels:[1,2,3,4,5,6,7,8,9,10]});
  const controls=document.createElement('div');controls.className='catalog-controls';
  controls.innerHTML='<label>Задача<select id="catalogTask">'+CATALOG.bundles.map(b=>`<option value="${escape(b.id)}">${escape(b.title)}</option>`).join('')+'</select></label><label id="catalogChannelLabel">Канал<select id="catalogChannel">'+S.registry.channels.map(c=>`<option value="${c.id}" ${c.id===9?'selected':''}>${c.id} · ${escape(c.name)}</option>`).join('')+'</select></label><div class="catalog-filter-row"><input id="catalogSearch" aria-label="Поиск по времени, каналу или имени файла" type="search" placeholder="Время, канал, файл" autocomplete="off"><select id="catalogAvailability" aria-label="Фильтр доступности"><option value="all">Все сроки</option><option value="ready">На диске</option><option value="download">Скачать</option><option value="problem">Ошибки</option></select></div><p class="micro catalog-key"><span class="catalog-channel local">●</span> На диске <span class="catalog-channel remote">○</span> В каталоге <span class="catalog-channel absent">—</span> Не найден</p><p id="catalogStatus" class="micro" role="status"></p>';
  const calendar=$('#calendar');
  if($('#catalogPanel .monthnav')){
    const details=document.createElement('details');details.id='catalogCalendar';
    const summary=document.createElement('summary');summary.textContent='Выбрать дату';details.append(summary);
    $('#catalogPanel .monthnav').before(details);
    for(const node of [$('#catalogPanel .monthnav'),$('#catalogPanel .weekdays'),calendar,$('#catalogPanel .calendar-bottom')])if(node)details.append(node);
    $('#dateOpen')?.addEventListener('click',()=>{details.open=true;});
  }
  $('#catalogPanel .section-heading').before(controls);
  const more=document.createElement('button');more.id='catalogMore';more.className='text-button full';more.textContent='Показать ещё 24 срока';more.hidden=true;$('#sessions').after(more);
  more.onclick=()=>{CATALOG.visible+=24;renderCatalog();};
  for(const id of ['#catalogTask','#catalogChannel','#catalogAvailability'])$(id).onchange=()=>{cancelCatalogOpen();CATALOG.visible=24;renderCatalog();};
  let timer;$('#catalogSearch').oninput=()=>{cancelCatalogOpen();clearTimeout(timer);timer=setTimeout(()=>{CATALOG.visible=24;renderCatalog();},120);};
  const search=document.createElement('label');search.textContent='Найти файл';search.innerHTML+='<input id="filesSearch" type="search" autocomplete="off" placeholder="Имя файла или канал">';$('#fileRows').before(search);
  const error=document.createElement('p');error.id='filesError';error.className='error-text';error.setAttribute('role','alert');error.hidden=true;$('#fileRows').before(error);
  $('#filesSearch').oninput=drawFiles;
  $('#fileCategory').onchange=()=>{S.selection.clear();drawFiles();};
  $('#selectChannels').textContent='Набор по задаче';
  $('#selectChannels').onclick=()=>{if(CATALOG.fileSession){S.selection=new Set(catalogPlan(CATALOG.fileSession).ids);$('#fileCategory').value='channel';$('#filesSearch').value='';drawFiles();}};
  $('#clearFiles').onclick=()=>{S.selection.clear();drawFiles();};
  $('#filesDialog').addEventListener('close',()=>{CATALOG.filesSequence++;CATALOG.fileSession=null;});
  $('#downloadSelected').onclick=async()=>{
    const ids=[...S.selection],sequence=CATALOG.filesSequence;if(!ids.length)return;
    const rows=S.fileAssets.filter(a=>S.selection.has(a.id)),bytes=rows.reduce((n,a)=>n+(a.size||0),0);
    if(bytes>=512*1024*1024&&!await ask('Загрузка большого набора',$('#fileSelection').textContent))return;
    if(sequence!==CATALOG.filesSequence)return;
    $('#downloadSelected').disabled=true;fileError('');
    try{await api('/api/queue',{ids});if(sequence===CATALOG.filesSequence){$('#filesDialog').close();toast('Добавлено в очередь: '+ids.length);}await loadDay();}
    catch(e){if(sequence===CATALOG.filesSequence)fileError(e.message);}
    finally{if(sequence===CATALOG.filesSequence)fileSelection();}
  };
  const help=document.createElement('a');help.href='/docs/CATALOG.html';help.target='_blank';help.rel='noopener';help.className='text-button';help.textContent='Как устроен каталог';controls.append(help);
}
