/* Рабочая карта: контекстное управление и объяснимый анализ исходных каналов.
   Без CDN, внешних шрифтов и фоновых запросов к сторонним картам. */
'use strict';
const UI = {
  leftMode: 'catalog', guides: {}, profiles: [], lastPoint: null, analysis: null,
  pointSequence: 0, mapSequence: 0, wantedBuild: null, buildTimer: null,
  activeBuild: null, resultSeen: '', profileText: '', routeRevision: 0,
  selectedScene: null, autoBuild: true, profileLoaded: false, contextEpoch: 0, activeEpoch: 0,
};
const original = {setMap, showProduct, selectScene, registerEvents, productHint, clearMapProduct, setDate, loadDay};
const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${escape(name)}"/></svg>`;
const readableDate = day => new Date(day+'T12:00:00Z').toLocaleDateString('ru-RU', {day:'numeric',month:'long',year:'numeric',timeZone:'UTC'}).replace(' г.','');
const displayCal = {unknown:'DN', assumed:'DN ≈ K', declared:'K', metadata:'K'};
const sourceCal = {unknown:'Единицы не объявлены',assumed:'DN = K: допущение',declared:'Задана калибровка',metadata:'Калибровка GeoTIFF'};

function ask(title, text) {
  const dialog = $('#confirmDialog');
  if(dialog.open) return Promise.resolve(false);
  $('#confirmTitle').textContent = title;
  $('#confirmText').textContent = text;
  return new Promise(resolve => {
    let done = false;
    const finish = yes => { if(done) return; done=true;dialog.close();resolve(yes); };
    $('#confirmYes').onclick=()=>finish(true);$('#confirmNo').onclick=()=>finish(false);
    dialog.oncancel=e=>{e.preventDefault();finish(false);};
    dialog.showModal();$('#confirmNo').focus();
  });
}

function left(mode='catalog', force=true) {
  const panel=$('#leftPanel');
  const visible=force || panel.hidden || UI.leftMode!==mode;
  panel.hidden=!visible;UI.leftMode=mode;
  $('#catalogPanel').hidden=mode!=='catalog';$('#productPanel').hidden=mode!=='product';
  $('#leftHeading').textContent=mode==='catalog'?'Снимки':'Продукты';
  $('#catalogRail').classList.toggle('active',visible&&mode==='catalog');
  $('#productRail').classList.toggle('active',visible&&mode==='product');
  $('#catalogRail').setAttribute('aria-expanded',String(visible&&mode==='catalog'));
  $('#productRail').setAttribute('aria-expanded',String(visible&&mode==='product'));
  if(visible&&innerWidth<1050) $('#inspector').hidden=true;
}

tab = function(name) {
  $('#inspector').hidden=false;
  $$('.tabs button').forEach(b=>{
    const active=b.dataset.tab===name;b.classList.toggle('active',active);
    b.setAttribute('aria-selected',String(active));
  });
  $$('.tab-panel').forEach(p=>p.hidden=p.id!=='tab-'+name);
  $('#inspectorHeading').textContent=name==='route'?'Маршрут':name==='legend'?'Легенда':'Анализ';
  $('#routeRail').classList.toggle('active',name==='route');
  if(innerWidth<1050){$('#leftPanel').hidden=true;$('#catalogRail').classList.remove('active');$('#productRail').classList.remove('active');}
};

function invalidateRoute() {
  UI.routeRevision++;S.route=null;$('#routeResult').replaceChildren();
}

function invalidatePoint() {
  UI.pointSequence++;UI.lastPoint=null;UI.analysis=null;
  $('#analysisExport').hidden=true;$('#analysisExport').removeAttribute('href');
  $('#profilePointResult').replaceChildren();
  $('#pointLayer').replaceChildren();$('#pointControls').hidden=true;
  $('#pixel').innerHTML=`<div class="inspector-empty">${icon('probe')}<h3>Исследуйте облачность</h3><p>Выберите точку на карте.</p></div>`;
  $('#cloudConfirmed').checked=false;$('#opaqueConfirmed').checked=false;
}

setDate = function(day) {
  if(!/^\d{4}-\d{2}-\d{2}$/.test(day)) return toast('Введите полную дату.');
  const test=new Date(day+'T00:00:00Z');
  if(!Number.isFinite(test.getTime())||test.toISOString().slice(0,10)!==day) return toast('Такой даты нет.');
  UI.contextEpoch++;UI.mapSequence++;clearTimeout(UI.buildTimer);UI.wantedBuild=null;invalidatePoint();invalidateRoute();
  if(S.product) clearMapProduct();
  $('#dateCaption').textContent=readableDate(day);$('#timeCaption').textContent='UTC';
  original.setDate(day);
};

clearMapProduct = function() {
  UI.contextEpoch++;UI.mapSequence++;original.clearMapProduct();invalidatePoint();invalidateRoute();$('#miniLegend').hidden=true;
  $('#mapTitle').textContent='Обзор Арктики';$('#productTag').textContent='Географическая основа';
};

function timeStrip() {
  $('#timeStrip').innerHTML=S.scenes.length?S.scenes.map(s=>`<button data-scene="${escape(s.id)}" class="${s.id===S.scene?.id?'active':''}" aria-pressed="${s.id===S.scene?.id}" data-tip="${escape((s.platform==='ARCM1'?'Арктика-М1':'Арктика-М2')+' · '+s.channels.length+' каналов')}">${escape(s.time.slice(11,16))}<small>${s.platform.slice(-1)}</small></button>`).join(''):'<span class="subtle">Выберите снимки</span>';
  $$('#timeStrip button').forEach(b=>b.onclick=()=>selectScene(S.scenes.find(s=>s.id===b.dataset.scene)));
  $('#previousScene').disabled=!S.scene||S.scenes.findIndex(s=>s.id===S.scene.id)<=0;
  $('#nextScene').disabled=!S.scene||S.scenes.findIndex(s=>s.id===S.scene.id)>=S.scenes.length-1;
  const active=$('#timeStrip button.active');if(active)active.scrollIntoView({block:'nearest',inline:'nearest'});
}

selectScene = function(scene) {
  const changed=S.scene?.id!==scene.id;
  original.selectScene(scene);UI.selectedScene=scene.id;
  $('#dateCaption').textContent=readableDate(scene.time.slice(0,10));$('#timeCaption').textContent=scene.time.slice(11,16)+' UTC';
  timeStrip();drawChannelChips();
  if(changed) {
    invalidatePoint();
    if(S.product&&S.product.scene_id!==scene.id) {
      S.product=null;$('#raster').removeAttribute('href');$('#nightRaster').removeAttribute('href');$('#miniLegend').hidden=true;$('#exportProduct').disabled=true;
    }
    if(UI.autoBuild) requestBuild();
  }
};

loadDay = async function() {
  await original.loadDay();timeStrip();
  // Single-click selects, a dedicated file button is discoverable with keyboard/touch.
  $$('#sessions .session').forEach(button=>{
    const key=button.dataset.key;button.setAttribute('data-tip','Открыть срок; файлы — двойной щелчок');
    const item=S.catalog.find(s=>s.platform+'|'+s.time===key);
    if(item){
      button.oncontextmenu=e=>{e.preventDefault();showFiles(item).catch(e=>toast(e.message));};
      const fileButton=document.createElement('button');fileButton.className='session-files text-button';fileButton.innerHTML=icon('folder')+'Каналы и файлы';fileButton.setAttribute('aria-label','Каналы и файлы '+item.time.slice(11,16));fileButton.onclick=()=>showFiles(item).catch(e=>toast(e.message));button.after(fileButton);
    }
  });
};

function drawChannelChips() {
  const have=new Set((S.scene?.channels||[]).map(c=>c.channel));
  $('#channelChips').innerHTML=S.registry.channels.filter(c=>have.has(c.id)).map(c=>`<button data-channel="${c.id}" class="${$('#product').value==='channel'&&Number($('#channel').value)===c.id?'active':''}" data-tip="${escape(c.wavelength_um+' мкм · '+c.name+' — '+c.description)}">${c.id}</button>`).join('');
  $$('#channelChips button').forEach(b=>b.onclick=()=>{
    $('#product').value='channel';$('#channel').value=b.dataset.channel;productHint();requestBuild();
  });
}

const TASKS=[
  {id:'micro24',icon:'cloud',name:'Облачность',hint:'Фаза и прозрачность'},
  {id:'channel',icon:'thermometer',name:'Оконный ИК',hint:'Канал 9 · Tя при калибровке'},
  {id:'night',icon:'fog',name:'Низкие облака',hint:'Ночная микрофизика'},
  {id:'difference',icon:'layers',name:'Разности',hint:'Оконный контраст'},
  {id:'phase',icon:'snow',name:'Фаза',hint:'Совместные признаки'},
  {id:'motion',icon:'satellite',name:'Динамика',hint:'Два срока'},
];

function tasks() {
  $('#taskGrid').innerHTML=TASKS.map(t=>`<button class="task-card" data-task="${t.id}">${icon(t.icon)}<span>${t.name}<small>${t.hint}</small></span></button>`).join('');
  $$('#taskGrid button').forEach(b=>b.onclick=()=>{
    if(b.dataset.task==='motion') {$('#motionOpen').click();return;}
    $('#product').value=b.dataset.task;
    if(b.dataset.task==='channel')$('#channel').value='9';
    productHint();requestBuild();
  });
}

productHint = function() {
  original.productHint();drawChannelChips();
  $$('#taskGrid button').forEach(b=>b.classList.toggle('active',b.dataset.task===$('#product').value));
  const guide=UI.guides[$('#product').value];
  if(guide)$('#productHint').textContent=guide.purpose;
};

function buildRequest() {
  if(!S.scene)return null;
  const body={scene:S.scene.id,product:$('#product').value,channel:Number($('#channel').value),preset:$('#preset').value,width:1000};
  if(body.product==='channel'){
    if($('#displayMin').value!=='')body.display_min=Number($('#displayMin').value);
    if($('#displayMax').value!=='')body.display_max=Number($('#displayMax').value);
  }
  return body;
}

function requestBuild() {
  const request=buildRequest();
  if(!request){left('catalog');return;}
  UI.contextEpoch++;UI.mapSequence++;invalidatePoint();invalidateRoute();
  UI.wantedBuild=request;clearTimeout(UI.buildTimer);
  UI.buildTimer=setTimeout(()=>launchBuild().catch(e=>toast(e.message)),180);
}

async function launchBuild() {
  if(!UI.wantedBuild||S.busy)return;
  const request=UI.wantedBuild;UI.wantedBuild=null;
  const item=S.registry.products.find(p=>p.id===request.product);
  const have=new Set(S.scene?.channels.map(c=>c.channel)||[]);
  const needed=request.product==='channel'?[request.channel]:(item?.channels||[]);
  const missing=needed.filter(c=>!have.has(c));
  if(missing.length){toast('Добавьте каналы '+missing.join(', '));left('catalog');return;}
  S.busy=true;UI.activeBuild=request;UI.activeEpoch=UI.contextEpoch;UI.resultSeen='';$('#mapBusy').hidden=false;$('#mapBusyText').textContent='Подготовка продукта';
  try {await api('/api/process',request);}
  catch(e) {S.busy=false;UI.activeBuild=null;$('#mapBusy').hidden=true;throw e;}
}

setMap = async function(preset,width=1000,product) {
  const sequence=++UI.mapSequence;
  const context=await api('/api/map?'+qs(product?{product}:{preset,width}));
  if(sequence!==UI.mapSequence)return null;
  S.map=context;const g=context.grid;S.view=[0,0,g.width,g.height];
  $('#map').setAttribute('viewBox',S.view.join(' '));
  for(const id of ['#ocean','#raster','#nightRaster','#clipRect']){$(id).setAttribute('width',g.width);$(id).setAttribute('height',g.height);}
  $('#graticule').replaceChildren(...context.graticule.map(l=>polyline(l,'graticule-line')));
  $('#coast').replaceChildren(...context.coast.map(l=>polyline(l,'coast-line')));
  $('#labels').replaceChildren(...context.labels.map(l=>{const t=svgEl('text',{x:l.x,y:l.y});t.textContent=l.text;return t;}));
  $('#projectionCaption').textContent=g.name;$('#projectionCaption').title=g.crs;
  return context;
};

showProduct = async function(p) {
  const epoch=UI.contextEpoch;
  if(UI.activeBuild&&UI.activeEpoch!==epoch)return;
  const current=buildRequest();
  if(UI.activeBuild&&current&&(p.scene_id!==current.scene||p.product!==current.product||p.grid.preset!==current.preset||Number(p.request.channel||9)!==current.channel))return;
  const keepView=S.map?.grid.crs===p.grid.crs&&S.map?.grid.preset===p.grid.preset&&S.map?.grid.width===p.grid.width;
  const view=keepView&&S.view?[...S.view]:null;
  const oldPoint=keepView&&S.product?.scene_id===p.scene_id?UI.lastPoint:null;
  const ctx=await setMap(p.grid.preset,p.grid.width,p.id);if(!ctx||epoch!==UI.contextEpoch)return;
  if(S.scene?.id!==p.scene_id){S.scene=S.scenes.find(s=>s.id===p.scene_id)||null;$('#dateCaption').textContent=readableDate(p.time.slice(0,10));$('#timeCaption').textContent=p.time.slice(11,16)+' UTC';}
  api('/api/settings',{view_settings:{preset:p.grid.preset,product:p.product,channel:p.request.channel||9}}).catch(e=>toast(e.message));
  S.product=p;$('#preset').value=p.grid.preset;$('#product').value=p.product;$('#channel').value=p.request.channel||9;
  $('#mapTitle').textContent=p.title.replace('Круглосуточная микрофизика','Микрофизика · 24 ч').replace('Интегральная карта спектральных признаков','Спектральные признаки');
  $('#mapSubtitle').textContent=p.platform+' · '+p.time;
  $('#productTag').textContent=(p.platform==='ARCM1'?'Арктика-М1':'Арктика-М2')+' · '+p.time.slice(11,16)+' UTC'+(p.calibration_status==='assumed'?' · DN ≈ K':'');
  $('#raster').setAttribute('href','/artifact/'+p.id+'/map.png');
  $('#nightRaster').setAttribute('href','/artifact/'+p.id+'/night.png');
  $('#exportProduct').disabled=false;$('#scienceBanner').textContent='';
  $('#motionLayer').replaceChildren();S.route=null;$('#routeResult').replaceChildren();
  invalidatePoint();drawLegend(p.legend);productHint();
  if(view){S.view=view;$('#map').setAttribute('viewBox',view.join(' '));}
  await redrawRouteDraft();
  if(oldPoint)await inspectAt(oldPoint.x,oldPoint.y);
};

function interpretedGuide(product) {return (S.product?.legend?.interpretation && S.product.product===product?S.product.legend.interpretation:UI.guides[product])||{};}
function legendSwatches(guide) {
  return (guide.swatches||[]).map(c=>`<article class="color-guide"><header><span class="swatch" style="background:${c.color}"></span>${escape(c.title)}</header><p>${escape(c.meaning)}</p><p class="guide-use">${escape(c.application)}</p></article>`).join('');
}

drawLegend = function(l) {
  let guide=interpretedGuide(l.product);if(l.product==='channel'&&l.units!=='K')guide={title:'Интенсивность сигнала',purpose:'Рисунок облачности и контраст. Для физических температур задайте шкалу каналов.',swatches:[]};const temperature=l.units==='K'&&l.product==='channel';
  const value=v=>temperature&&v!==null?v-273.15:v;
  const unit=temperature?'°C':l.units;
  let html=`<h3>${escape(guide.title||l.title)}</h3><p class="hint">${escape(guide.purpose||l.meaning||'')}</p>`;
  if(l.status==='assumed')html+=`<div class="caveat">${icon('tune')}<span>DN = K — принятое допущение</span></div>`;
  html+=legendSwatches(guide);
  let miniature='';
  if(l.palette) {
    const grad=l.palette.map(c=>'rgb('+c.join(',')+')').join(',');
    const bar=`<div class="colorbar" style="background:linear-gradient(to right,${grad})"></div><div class="scale-labels"><span>${num(value(l.display_min))} ${escape(unit)}</span><span>${num(value(l.display_max))} ${escape(unit)}</span></div>`;
    html+=bar;miniature=bar;
  }
  if(l.stats)html+=`<div class="metric-grid"><div class="metric"><small>Минимум в области</small><b>${num(value(l.stats.min))} <small>${unit}</small></b></div><div class="metric"><small>Максимум в области</small><b>${num(value(l.stats.max))} <small>${unit}</small></b></div></div>`;
  if(l.classes){
    html+=l.classes.map(c=>`<div class="legend-item"><span class="swatch" style="background:${c.color}"></span><span>${escape(c.name)}</span></div>`).join('');
    miniature=`<div class="mini-swatches">${l.classes.filter(c=>c.value>0).slice(0,3).map(c=>`<span><i style="background:${c.color}"></i>${escape(c.name.split(':')[0])}</span>`).join('')}</div>`;
  }
  if(guide.swatches?.length)miniature=`<div class="mini-swatches">${guide.swatches.map(c=>`<span><i style="background:${c.color}"></i>${escape(c.title.split(' / ')[0])}</span>`).join('')}</div>`;
  html+='<details><summary>Компоненты, шкалы и качество</summary>';
  if(l.components)html+=l.components.map(c=>`<div class="component" style="border-color:${c.component==='R'?'#bf6576':c.component==='G'?'#43977e':'#5587c7'}"><b>${c.component}</b> <code>${escape(c.formula)}</code><small>${c.min}…${c.max} K · γ ${c.gamma}</small><small>В области: ${num(c.stats.min)}…${num(c.stats.max)} K</small></div>`).join('');
  html+=`<p class="micro">${escape(l.data_stats_scope||'')}<br>${escape(l.nodata||'')}<br>Калибровка: ${escape(sourceCal[l.status]||l.status)}.</p><pre>${escape(JSON.stringify({flags:l.quality_flags,version:l.version},null,2))}</pre></details><a class="export-link" href="/docs/ANALYSIS.md" target="_blank" rel="noopener">Методика интерпретации</a>`;
  $('#legend').innerHTML=html;
  $('#miniLegend').innerHTML=`<div class="mini-legend-title">${icon('info')}${escape(guide.title||l.title)}</div>${miniature}`;
  $('#miniLegend').hidden=false;
};

mapClick = async function(e) {
  if(!S.product){left('catalog');return;}
  const p=pointerPoint(e);
  if(p.x<0||p.y<0||p.x>=S.map.grid.width||p.y>=S.map.grid.height)return;
  if(S.drawing) {
    const c=await api('/api/coordinates',{x:p.x,y:p.y,product:S.product.id});
    const previous=$('#routePoints').value.trim();
    $('#routePoints').value=(previous?previous+'\n':'')+c.lon.toFixed(5)+', '+c.lat.toFixed(5);
    await redrawRouteDraft();return;
  }
  await inspectAt(p.x,p.y);
};

function pointRequest() {
  if(!UI.lastPoint||!S.product)return null;
  return {product:S.product.id,x:UI.lastPoint.x,y:UI.lastPoint.y,
    profile_id:$('#analysisProfile').value||undefined,
    altitude_m:$('#analysisAltitude').value===''?null:Number($('#analysisAltitude').value),
    delta_k:Number($('#analysisDelta').value), cloud_confirmed:$('#cloudConfirmed').checked,
    opaque_confirmed:$('#opaqueConfirmed').checked};
}

async function inspectAt(x,y,keep=false) {
  const id=++UI.pointSequence;UI.lastPoint={x,y};UI.analysis=null;
  $('#analysisExport').hidden=true;$('#analysisExport').removeAttribute('href');
  $('#profilePointResult').replaceChildren();$('#pixel').innerHTML='<p class="hint">Расчёт…</p>';
  if(!keep){$('#cloudConfirmed').checked=false;$('#opaqueConfirmed').checked=false;}
  tab('point');$('#pixel').setAttribute('aria-busy','true');
  const body=pointRequest();let result;try{result=await api('/api/analyse',body);}finally{if(id===UI.pointSequence)$('#pixel').setAttribute('aria-busy','false');}
  if(id!==UI.pointSequence||S.product?.id!==body.product)return;
  UI.analysis=result;drawPoint(result);
  const scale=S.view?S.view[2]/S.map.grid.width:1;
  $('#pointLayer').replaceChildren(svgEl('circle',{cx:x,cy:y,r:7*scale,fill:'none',stroke:'#fff','stroke-width':2*scale}),svgEl('circle',{cx:x,cy:y,r:2*scale,fill:'#fff'}));
  $('#pixel').setAttribute('aria-busy','false');
}

function drawPoint(r) {
  const p=r.pixel,a=r.analysis;
  const coordinates=`${num(Math.abs(p.lat),3)}° ${p.lat>=0?'с.':'ю.'} ш. · ${num(Math.abs(p.lon),3)}° ${p.lon>=0?'в.':'з.'} д.`;
  let html=`<div class="point-heading"><h3>${coordinates}</h3><span class="pill" data-tip="${escape(sourceCal[a.calibration])}">${escape(displayCal[a.calibration]||'K')}</span></div>`;
  const featured=['t9','d97','d109','d94'];
  if(a.metrics.length)html+=`<div class="metric-grid">${featured.map(id=>a.metrics.find(m=>m.id===id)).filter(Boolean).map(m=>`<div class="metric ${m.id==='t9'?'primary-metric':''}" tabindex="0" data-tip="${escape(m.explanation)}"><small>${escape(m.label)}</small><b>${num(m.value)} <small>${m.units}</small></b></div>`).join('')}</div>`;
  html+=a.cards.map(c=>`<article class="evidence-card ${escape(c.tone)}"><div class="evidence-card-head">${icon(c.icon)}<h3>${escape(c.title)}</h3></div><p>${escape(c.meaning)}</p>${c.evidence.length?`<details><summary>На чём основано</summary>${c.evidence.map(t=>`<div class="evidence-row">${escape(t)}</div>`).join('')}<p class="micro">Пороговая гипотеза МСУ-ГС/А; не калиброванная вероятность.</p></details>`:''}<div class="action">${escape(c.action)}</div></article>`).join('');
  html+=`<details><summary>Все каналы и исходные значения</summary><table><thead><tr><th>Канал</th><th>Tя / DN</th><th>Единицы</th></tr></thead><tbody>${p.channels.map(c=>`<tr><td>${c.channel}<small>${channelMeta(c.channel)?.wavelength_um} мкм</small></td><td>${num(c.unit==='K'&&c.value!==null?c.value-273.15:c.value)}</td><td>${c.unit==='K'?'°C':escape(c.unit)}</td></tr>`).join('')}</tbody></table><p class="micro">Солнце: ${num(p.sun_elevation)}° · флаги продукта: ${p.quality_flags}. Спектральные разности вычислены из исходных пикселей.</p></details>`;
  $('#pixel').innerHTML=html;$('#pointControls').hidden=false;
  $('#analysisExport').href='/analysis-export/'+r.id;$('#analysisExport').hidden=false;
  drawProfilePoint(r.profile_result);
}

function drawProfilePoint(r) {
  if(!r){$('#profilePointResult').innerHTML='<p class="hint">Добавьте профиль T(z) для высоты. Для условий обледенения в слое нужен также жидкий конденсат.</p>';return;}
  const src=r.profile;
  let html=`<span class="profile-source">${escape(src.source)} · ${escape(src.valid_time.replace('T',' ').replace('Z',' UTC'))}<br>${num(src.distance_km,0)} км до профиля · расхождение ${num(src.time_gap_hours)} ч</span>`;
  if(!src.applicable){$('#profilePointResult').innerHTML=html+`<div class="caveat">${icon('info')}<span>${src.reasons.map(escape).join('<br>')}</span></div>`;return;}
  if(r.profile_plot)html+=profileChart(r);
  const h=r.height;
  html+=`<section class="profile-result"><h3>${icon('height')} ${escape(h.title)}</h3>`;
  if(h.candidate_heights_m){
    html+=`<div class="profile-value">${h.candidate_heights_m.length?h.candidate_heights_m.map(v=>num(v/1000,2)).join(' / '):'—'} <small>км AMSL</small></div>`;
    if(h.ambiguous)html+='<p class="hint">Неоднозначное соответствие T(z); единственная высота не выбрана.</p>';
    html+=`<p class="micro">При Tя ± ${num(h.delta_k)} K: ${(h.sensitivity_ranges_m||[]).map(x=>num(x[0]/1000,2)+'–'+num(x[1]/1000,2)+' км').join('; ')||'нет пересечения'}. Диапазон чувствительности, не доверительный интервал.</p>`;
    html+=`<details><summary>Допущения высоты</summary><p class="micro">${escape(h.method)}. Атмосферная поправка не выполнена. Облачность и непрозрачность подтверждены оператором только для этой точки.</p></details>`;
  }
  html+='</section>';
  const l=r.layer;
  html+=`<section class="profile-result"><h3>На ${num(l.altitude_m||Number($('#analysisAltitude').value),0)} м</h3><div class="layer-state ${l.status==='supercooled'?'supercooled':''}">${escape(l.title)}</div>`;
  if(l.temperature_c!==undefined)html+=`<div class="metric-grid"><div class="metric"><small>Температура воздуха</small><b>${num(l.temperature_c)} <small>°C</small></b></div><div class="metric"><small>Жидкая вода</small><b>${num(l.liquid_water_g_m3,3)} <small>г/м³</small></b></div></div>`;
  if(l.cloud_liquid_kg_kg!==undefined)html+=`<details><summary>Основание оценки слоя</summary><p class="micro">qₗ = ${num(l.cloud_liquid_kg_kg*1e6,2)} мг/кг. Порог выделения: ${num(l.liquid_floor_kg_kg*1e6,2)} мг/кг. Плотность: ${escape(l.density_method||'не определена')}. Оценка не задаёт интенсивность обледенения, размер капель или безопасность полёта.</p></details>`;
  html+='</section>';
  if(r.column)html+=`<section class="profile-result"><h3>В заданном столбе профиля</h3><div class="metric-grid"><div class="metric"><small>Водяной пар</small><b>${num(r.column.vapor_mm)} <small>мм</small></b></div><div class="metric"><small>Перенос влаги</small><b>${num(r.column.ivt_kg_m_s,0)} <small>кг/(м·с)</small></b></div></div><p class="micro">${r.column.pressure_range_pa.map(v=>num(v/100,0)).join('–')} гПа · из профиля, не из цвета снимка.</p></section>`;
  $('#profilePointResult').innerHTML=html;
}

function profileChart(result) {
  const d=result.profile_plot,z=d.height_m,t=d.temperature_c;
  let lo=Math.floor(Math.min(...t)/10)*10,hi=Math.ceil(Math.max(...t)/10)*10;if(lo===hi)hi=lo+10;
  const bottom=z[0],top=z[z.length-1];
  const x=v=>40+(v-lo)/(hi-lo)*230,y=v=>190-(v-bottom)/(top-bottom)*166;
  const path=t.map((v,i)=>x(v)+','+y(z[i])).join(' ');
  const h=result.height;
  const layer=result.layer;
  let guides='';
  for(let v=lo;v<=hi;v+=10)guides+=`<line x1="${x(v)}" y1="24" x2="${x(v)}" y2="190" stroke="#e0e6ef"/><text x="${x(v)}" y="207" text-anchor="middle" fill="#65768b" font-size="10">${v}</text>`;
  for(let j=0;j<=3;j++){const v=bottom+j*(top-bottom)/3;guides+=`<line x1="40" y1="${y(v)}" x2="270" y2="${y(v)}" stroke="#e0e6ef"/><text x="35" y="${y(v)+3}" text-anchor="end" fill="#65768b" font-size="10">${num(v/1000,1)}</text>`;}
  if(layer.temperature_c!==undefined)guides+=`<line x1="40" y1="${y(layer.altitude_m)}" x2="270" y2="${y(layer.altitude_m)}" stroke="#2d8e80" stroke-dasharray="4 3"/><circle cx="${x(layer.temperature_c)}" cy="${y(layer.altitude_m)}" r="4" fill="#2d8e80"/>`;
  return `<svg class="profile-chart" viewBox="0 0 300 224" role="img" aria-label="Температурный профиль: высота в километрах, температура в градусах Цельсия"><text x="40" y="13" fill="#4a627c" font-size="11">T(z) · ${escape(result.profile.source.slice(0,24))}</text>${guides}<polyline points="${path}" fill="none" stroke="#345dca" stroke-width="2"/><text x="250" y="220" fill="#65768b" font-size="10">°C</text><text x="4" y="14" fill="#65768b" font-size="10">км</text></svg>`;
}

async function refreshProfiles(selectId) {
  const response=await api('/api/profiles');UI.profiles=response.profiles;
  for(const id of ['#analysisProfile','#routeProfile']){
    const old=selectId||$(id).value;
    $(id).innerHTML='<option value="">Не выбран</option>'+UI.profiles.map(p=>`<option value="${p.id}">${escape(p.source+' · '+p.valid_time.slice(11,16))}</option>`).join('');
    if(UI.profiles.some(p=>p.id===old))$(id).value=old;
  }
  $('#profileList').innerHTML=UI.profiles.map(p=>`<article class="profile-list-item"><h3>${escape(p.source)}</h3><p>${escape(p.valid_time.replace('T',' ').replace('Z',' UTC'))}</p><p>${num(p.lat,2)}°, ${num(p.lon,2)}° · ${p.radius_km} км / ${p.max_hours} ч</p><button class="text-button" data-profile="${p.id}">Использовать</button></article>`).join('')||'<p class="empty">Добавьте наблюдательный или модельный профиль для температуры в слое и оценки высоты.</p>';
  $$('#profileList button').forEach(b=>b.onclick=()=>{invalidateRoute();$('#analysisProfile').value=b.dataset.profile;$('#routeProfile').value=b.dataset.profile;$('#profilesDialog').close();if(UI.lastPoint)inspectAt(UI.lastPoint.x,UI.lastPoint.y,true).catch(e=>toast(e.message));});
}

async function showProfiles() {await refreshProfiles();open('#profilesDialog');if(!UI.profiles.length)$('#profileImportDetails').open=true;}

async function profileFileChanged() {
  const file=$('#profileFile').files[0];if(!file)return;
  if(file.size>1_000_000)throw Error('Профиль больше 1 МБ.');
  UI.profileText=await file.text();$('#profileFileName').textContent=file.name;
  for(const id of ['#profileSource','#profileLat','#profileLon','#profileTime'])$(id).value='';
  $('#profileRadius').value=100;$('#profileHours').value=3;
  $('#profileTime').step=1;
  if(UI.profileText.trim().startsWith('{')){
    const p=JSON.parse(UI.profileText);
    for(const [field,selector] of [['source','#profileSource'],['lat','#profileLat'],['lon','#profileLon'],['radius_km','#profileRadius'],['max_hours','#profileHours']])if(p[field]!==undefined)$(selector).value=p[field];
    if(p.valid_time){const stamp=String(p.valid_time).trim();if(/(?:Z|[+-]\\d{2}:?\\d{2})$/i.test(stamp)){const d=new Date(stamp);if(Number.isFinite(d.getTime()))$('#profileTime').value=d.toISOString().slice(0,19);}}
  }
}

async function importProfile() {
  if(!UI.profileText)throw Error('Выберите файл CSV или JSON.');
  const metadata={source:$('#profileSource').value,lat:$('#profileLat').value,lon:$('#profileLon').value,
    valid_time:$('#profileTime').value?$('#profileTime').value+($('#profileTime').value.length===16?':00Z':'Z'):'',radius_km:$('#profileRadius').value,max_hours:$('#profileHours').value};
  const r=await api('/api/profiles/import',{text:UI.profileText,metadata});
  invalidateRoute();await refreshProfiles(r.id);$('#profileImportDetails').open=false;toast('Профиль добавлен');
  if(UI.lastPoint)await inspectAt(UI.lastPoint.x,UI.lastPoint.y,true);
}

function setDrawing(value) {
  S.drawing=value;$('#drawingPill').hidden=!value;
  $('#drawRoute').classList.toggle('draw-active',value);$('#drawRoute').innerHTML=icon('route')+(value?'Точки на карте':'Нарисовать');
  if(value){tab('route');if(innerWidth<720)$('#inspector').hidden=true;}
}

async function redrawRouteDraft() {
  invalidateRoute();
  let points=[];try{points=routePoints();}catch{
    $('#routeLayer').replaceChildren();$('#routeWaypointList').replaceChildren();return;
  }
  $('#routeWaypointList').innerHTML=points.map((p,i)=>`<div class="waypoint"><span class="waypoint-number">${i+1}</span><input aria-label="Долгота и широта точки ${i+1}" data-index="${i}" value="${num(p[0],3).replace(',','.')}, ${num(p[1],3).replace(',','.')}" data-tip="Долгота, широта. Enter — применить."><button class="icon-button" data-remove="${i}" aria-label="Удалить точку ${i+1}">${icon('delete')}</button></div>`).join('');
  $$('#routeWaypointList input').forEach(input=>{
    const commit=()=>{const numbers=input.value.trim().split(/[,;\s]+/).map(Number);if(numbers.length!==2||numbers.some(v=>!Number.isFinite(v))||Math.abs(numbers[0])>180||Math.abs(numbers[1])>90){toast('Введите долготу и широту через запятую.');return;}points[Number(input.dataset.index)]=numbers;$('#routePoints').value=points.map(p=>p.join(', ')).join('\n');redrawRouteDraft().catch(e=>toast(e.message));};
    input.onchange=commit;input.onkeydown=e=>{if(e.key==='Enter')input.blur();};
  });
  $$('#routeWaypointList [data-remove]').forEach(b=>b.onclick=()=>{points.splice(Number(b.dataset.remove),1);$('#routePoints').value=points.map(p=>p.join(', ')).join('\n');redrawRouteDraft().catch(e=>toast(e.message));});
  if(!S.product){$('#routeLayer').replaceChildren();return;}
  const seq=++UI.routeRevision;
  if(!points.length){$('#routeLayer').replaceChildren();return;}
  const r=await api('/api/project-points',{product:S.product.id,points});if(seq!==UI.routeRevision)return;
  const lines=[];let current=[];
  for(const p of (r.line||r.points)){if(!p.every(Number.isFinite)){if(current.length)lines.push(current);current=[];continue;}if(current.length&&Math.abs(current.at(-1)[0]-p[0])>S.map.grid.width*.6){lines.push(current);current=[];}current.push(p);}
  if(current.length)lines.push(current);
  const markers=r.points.flatMap((p,i)=>p.every(Number.isFinite)?[svgEl('circle',{cx:p[0],cy:p[1],r:6,class:'route-point','data-route-index':i}),(()=>{const t=svgEl('text',{x:p[0]+10,y:p[1]-7});t.textContent=i+1;return t;})()]:[]);
  $('#routeLayer').replaceChildren(...lines.filter(l=>l.length>1).map(l=>polyline(l,'route-line')),...markers);
  enableWaypointDrag();
  S.route=null;$('#routeResult').replaceChildren();
}

function enableWaypointDrag() {
  $$('#routeLayer circle[data-route-index]').forEach(marker=>{
    marker.setAttribute('tabindex','0');marker.setAttribute('role','button');marker.setAttribute('aria-label','Точка '+(Number(marker.dataset.routeIndex)+1)+'. Перетащить или Enter для координат.');
    marker.onkeydown=e=>{if(e.key==='Enter'){tab('route');$('#routeWaypointList input[data-index="'+marker.dataset.routeIndex+'"]').focus();}};
    marker.onpointerdown=e=>{
      if(e.button!==0)return;e.stopPropagation();drag=null;
      marker.setPointerCapture(e.pointerId);marker.dataset.dragging='true';
    };
    marker.onpointermove=e=>{
      if(marker.dataset.dragging!=='true')return;e.stopPropagation();const p=pointerPoint(e);
      marker.setAttribute('cx',p.x);marker.setAttribute('cy',p.y);
    };
    marker.onpointercancel=()=>{delete marker.dataset.dragging;redrawRouteDraft().catch(e=>toast(e.message));};
    marker.onpointerup=async e=>{
      if(marker.dataset.dragging!=='true')return;e.stopPropagation();delete marker.dataset.dragging;
      try{
        const p=pointerPoint(e),c=await api('/api/coordinates',{product:S.product.id,x:p.x,y:p.y});
        const points=routePoints();points[Number(marker.dataset.routeIndex)]=[c.lon,c.lat];
        $('#routePoints').value=points.map(p=>p.map(n=>n.toFixed(5)).join(', ')).join('\n');
      }catch(err){toast(err.message);}finally{await redrawRouteDraft();}
    };
  });
}

calculateRoute = async function() {
  if(!S.product)throw Error('Откройте снимок для маршрута.');
  const product=S.product.id;const rev=UI.routeRevision;
  const data={product,points:routePoints(),step_km:Number($('#routeStep').value),speed_kmh:Number($('#speed').value),
    departure:$('#departure').value?$('#departure').value+':00Z':'',
    profile_id:$('#routeProfile').value||undefined,altitude_m:$('#routeAltitude').value===''?null:Number($('#routeAltitude').value)};
  $('#calculateRoute').disabled=true;
  try{
    const r=await api('/api/route',data);
    if(S.product?.id!==product||rev!==UI.routeRevision)return;
    S.route=r;setDrawing(false);
    $('#routeResult').innerHTML=`<div class="section-heading"><h3>${num(r.length_km,0)} км</h3><span class="micro">${r.samples.length} точек</span></div>${routeChart(r)}<div class="row"><a href="/route-export/${r.id}/csv">CSV</a><a href="/route-export/${r.id}/geojson">GeoJSON</a><a href="/route-export/${r.id}/json">Расчёты</a></div><div class="route-table"><table><thead><tr><th>Км</th><th>Tя9, °C</th><th>Слой</th></tr></thead><tbody>${r.samples.map((x,i)=>`<tr tabindex="0" data-sample="${i}" data-tip="Исследовать точку маршрута"><td>${num(x.distance_km,0)}<small>${escape(x.eta?.slice(11,16)||'')}</small></td><td>${num(x.metrics?.t9)}</td><td>${escape(x.profile_result?.layer?.status==='supercooled'?'Переохл. жидкость':x.profile_result?.profile?.applicable===false?'Вне профиля':x.profile_result?.layer?.temperature_c!==undefined?num(x.profile_result.layer.temperature_c)+' °C':'—')}</td></tr>`).join('')}</tbody></table></div><details><summary>Основание маршрутного анализа</summary><p class="micro">Спектральные признаки относятся к наблюдению. Для профильных условий используется время прохождения и индивидуальная проверка радиуса/срока каждой точки. Возраст наблюдения и отсутствие данных сохранены в экспорте.</p></details>`;
    $$('#routeResult [data-sample]').forEach(row=>{const openPoint=()=>{const xy=r.map_points[Number(row.dataset.sample)];if(xy?.every(Number.isFinite))inspectAt(...xy).catch(e=>toast(e.message));};row.onclick=openPoint;row.onkeydown=e=>{if(e.key==='Enter')openPoint();};});
  }finally{$('#calculateRoute').disabled=false;}
};

const baseChart=routeChart;
routeChart=function(r){
  const copy={...r,units:structuredClone(r.units),samples:r.samples.map(s=>({...s,channels:{...s.channels}}))};
  for(const ch of Object.keys(copy.units))if(copy.units[ch].unit==='K'){copy.units[ch].unit='°C';for(const row of copy.samples)if(row.channels[ch]!==null)row.channels[ch]-=273.15;}
  return baseChart(copy);
};

function installTooltips() {
  const tip=$('#tooltip');let timer,owner;
  function hide(){clearTimeout(timer);tip.hidden=true;if(owner)owner.removeAttribute('aria-describedby');owner=null;}
  function show(target){
    hide();owner=target;
    timer=setTimeout(()=>{
      if(!target.isConnected)return;tip.textContent=target.dataset.tip||target.title||'';
      if(!tip.textContent)return;tip.hidden=false;target.setAttribute('aria-describedby','tooltip');
      const b=target.getBoundingClientRect();const w=tip.offsetWidth,h=tip.offsetHeight;
      tip.style.left=Math.min(innerWidth-w-8,Math.max(8,b.left+b.width/2-w/2))+'px';
      tip.style.top=(b.bottom+h+12<innerHeight?b.bottom+9:Math.max(8,b.top-h-9))+'px';
    },350);
  }
  document.addEventListener('pointerover',e=>{const t=e.target.closest('[data-tip]');if(t&&owner!==t)show(t);});
  document.addEventListener('pointerout',e=>{if(owner&&!owner.contains(e.relatedTarget))hide();});
  document.addEventListener('focusin',e=>{const t=e.target.closest('[data-tip]');if(t)show(t);});
  document.addEventListener('focusout',hide);
  document.addEventListener('keydown',e=>{if(e.key==='Escape')hide();});
  document.addEventListener('pointerdown',hide);document.addEventListener('scroll',hide,true);
}

poll = async function() {
  try{
    const buildAtRequest=UI.activeBuild;const r=await api('/api/state');if(UI.activeBuild!==buildAtRequest){return;}S.serverState=r;
    S.busy=r.busy||Boolean(UI.activeBuild&&r.result===null);
    $('#status').textContent=r.busy?r.status:'Готово';$('#statusdot').classList.toggle('busy',r.busy);
    $('#cancel').hidden=!r.busy;$('#process').disabled=r.busy;
    $('#calibrationState').textContent=displayCal[r.calibration?.mode]||'DN';
    $('#calibrationOpen').dataset.tip=sourceCal[r.calibration?.mode]||'Единицы каналов';
    $('#downloadPath').textContent=r.download_dir;
    const q=r.queue_counts||{},count=(q.queued||0)+(q.running||0);
    $('#queueCount').textContent=count;$('#queueCount').hidden=!count;
    $('#authOpen').classList.toggle('tonal',r.token.present);$('#authOpen').dataset.tip=r.token.present?'GPTL подключён':'Подключить GPTL';
    $('#tokenStatus').innerHTML=`<p class="micro">Bearer: ${r.token.present?(r.token.remaining==null?'срок неизвестен':r.token.remaining>0?num(r.token.remaining/60,0)+' мин':'истёк'):'не задан'} · STS: ${r.token.sts_expires?'получен':'нет'} · обновление: ${r.oauth?.refresh_present?'доступно':'не задано'}</p>`;
    $('#mapBusy').hidden=!UI.activeBuild;
    if(UI.activeBuild)$('#mapBusyText').textContent=r.busy?r.status:'Завершение';
    if(r.revision!==S.revision){S.revision=r.revision;await loadDay();await loadCalendar();}
    const resultKey=r.result?JSON.stringify(r.result):'';
    if(!r.busy&&r.result&&resultKey!==UI.resultSeen){
      UI.resultSeen=resultKey;
      if(r.result.ok&&r.result.result?.legend){
        if(!UI.wantedBuild&&(!UI.activeBuild||UI.activeEpoch===UI.contextEpoch))await showProduct(r.result.result);
      }else if(r.result.ok&&r.result.result?.vectors){
        drawMotion(r.result.result);$('#motionResult').textContent='Векторов: '+r.result.result.vectors.length+'; кандидатов: '+r.result.result.candidates.length;
      }else if(!r.result.ok){
        toast(r.result.error);
        if(/калибров|температур/.test(r.result.error)&&UI.activeBuild)$('#calibrationOpen').click();
      }
      UI.activeBuild=null;S.busy=false;$('#mapBusy').hidden=true;
    }
    if(!S.busy&&UI.wantedBuild)await launchBuild();
    if($('#queueDialog').open)await refreshQueue();
  }catch(e){$('#status').textContent=e.message;}
  finally{setTimeout(poll,1000);}
};

registerEvents = function() {
  original.registerEvents();
  bind('#catalogRail',()=>left('catalog',false));bind('#dateOpen',()=>left('catalog'));
  bind('#productRail',()=>left('product',false));bind('#productQuick',()=>left('product'));
  bind('#closeLeft',()=>{left(UI.leftMode,false);});
  bind('#routeRail',()=>{tab('route');});
  bind('#closeInspector',()=>{$('#inspector').hidden=true;$('#routeRail').classList.remove('active');});
  bind('#legendOpen',()=>tab('legend'));bind('#miniLegend',()=>tab('legend'));
  bind('#profilesOpen',showProfiles);bind('#pointProfileAdd',showProfiles);
  bind('#importProfile',importProfile);
  $('#profileFile').onchange=()=>profileFileChanged().catch(e=>toast(e.message));
  bind('#calculatePoint',()=>{if(UI.lastPoint)return inspectAt(UI.lastPoint.x,UI.lastPoint.y,true);});
  $('#analysisProfile').onchange=()=>{if(UI.lastPoint)inspectAt(UI.lastPoint.x,UI.lastPoint.y,true).catch(e=>toast(e.message));};
  bind('#assumptionInfo',()=>ask('Облачность и непрозрачность','Подтверждение относится только к выбранному пикселю. Оно должно опираться на дополнительный анализ или независимую маску, а не только на цвет. Для высоты принимается Tя9 = T вершины; атмосферная поправка не выполнена.'));
  // Replace rather than stack actions registered by the previous UI.
  $('#product').onchange=()=>{productHint();requestBuild();};
  $('#channel').onchange=()=>{productHint();requestBuild();};
  $('#preset').onchange=async()=>{
    clearMapProduct();await setMap($('#preset').value);requestBuild();
  };
  $('#displayMin').onchange=requestBuild;$('#displayMax').onchange=requestBuild;
  const oldDraw=$('#drawRoute'),newDraw=oldDraw.cloneNode(true);oldDraw.replaceWith(newDraw);
  bind('#drawRoute',()=>{if(!S.product){left('catalog');return;}setDrawing(!S.drawing);});
  const oldUndo=$('#undoRoute'),newUndo=oldUndo.cloneNode(true);oldUndo.replaceWith(newUndo);
  bind('#undoRoute',async()=>{const rows=$('#routePoints').value.trim().split('\n');rows.pop();$('#routePoints').value=rows.join('\n');await redrawRouteDraft();});
  const oldClear=$('#clearRoute'),newClear=oldClear.cloneNode(true);oldClear.replaceWith(newClear);
  bind('#clearRoute',async()=>{$('#routePoints').value='';await redrawRouteDraft();});
  bind('#finishDrawing',()=>{setDrawing(false);tab('route');});
  $('#routePoints').onchange=()=>redrawRouteDraft().catch(e=>toast(e.message));
  for(const id of ['#departure','#speed','#routeStep','#routeProfile','#routeAltitude'])$(id).addEventListener('change',invalidateRoute);
  for(const id of ['#analysisAltitude','#analysisDelta','#cloudConfirmed','#opaqueConfirmed'])$(id).addEventListener('change',()=>{
    if(UI.lastPoint)inspectAt(UI.lastPoint.x,UI.lastPoint.y,true).catch(e=>toast(e.message));
  });
  $('#map').addEventListener('pointercancel',()=>{drag=null;});
  $('#map').addEventListener('keydown',e=>{
    if(['+','=','-','0','ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)){
      e.preventDefault();
      if(e.key==='+'||e.key==='=')zoom(.8);else if(e.key==='-')zoom(1.25);else if(e.key==='0')$('#mapReset').click();
      else{const step=S.view[2]*.07;if(e.key==='ArrowLeft')S.view[0]-=step;if(e.key==='ArrowRight')S.view[0]+=step;if(e.key==='ArrowUp')S.view[1]-=step;if(e.key==='ArrowDown')S.view[1]+=step;$('#map').setAttribute('viewBox',S.view.join(' '));}
    }
  });
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape'&&!$$('dialog').some(d=>d.open)){if(S.drawing)setDrawing(false);else $('#inspector').hidden=true;}
  });
  // Calibration changes require a new immutable product before a point is interpreted.
  $('#calibrationDialog').addEventListener('close',()=>{if(UI.calSaved&&S.scene){UI.calSaved=false;requestBuild();}});
  tasks();installTooltips();
};

(async()=>{
  await boot();
  UI.guides=await api('/api/guides');await refreshProfiles();productHint();
  if(S.product)drawLegend(S.product.legend);
})().catch(e=>{toast(e.message);$('#status').textContent='Откройте полный адрес из окна сервера. '+e.message;});
