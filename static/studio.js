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

// Сохраняем существующие элементы и контракты API, группируя дополнительные
// параметры по задаче. Профиль не является условием просмотра снимка.
function workflowLayout() {
  const help='/docs/QUICKSTART.html';
  const style=document.createElement('style');
  style.id='workflow-styles';
  style.textContent=`
    .task-card>span{display:block;min-width:0;max-width:100%;overflow-wrap:anywhere}
    .task-card small{display:block;margin-top:5px}
    .task-card:disabled{opacity:.68}
    #routeWaypointList{max-height:180px;overflow:auto;overscroll-behavior:contain}
    .waypoint .icon-button{opacity:1}
    #routeHint{margin-top:0}
    #calculateRoute{white-space:normal;line-height:1.4}
    #routeOptions,#routeProfileOptions{margin-top:10px}
    #routeResult .route-table{margin-top:16px;max-height:320px}
    #routeResult th{white-space:normal;min-width:68px}
    #routeResult .row a{display:inline-block;padding:7px 0}
    #pointControls>.section-heading{flex-wrap:wrap}
    #profileList:empty{display:none}
    #profilePreview{padding:12px 14px;border-radius:12px;background:var(--bg);font-size:13px;line-height:1.65;overflow-wrap:anywhere}
    #profileMetadata[open],#profileLimits[open]{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 16px}
    #profileMetadata>summary,#profileLimits>summary,#profileLimits>p{grid-column:1/-1}
    #profileFileName{overflow-wrap:anywhere;font-size:12px}
    #profileImportError{white-space:normal}
    #profileImportDetails a{overflow-wrap:anywhere}
    #profileImportDetails input[aria-invalid=true]{border-color:#a23b51}
    #profileFile{width:100%;max-width:100%}
    #profileFile::file-selector-button{font:inherit;padding:9px 14px;border:1px solid var(--line);border-radius:10px;background:var(--tonal);color:var(--primary);margin-right:12px;cursor:pointer}
    #profileList:empty+#profileImportDetails{border-top:0;margin-top:0}
    @media(max-width:600px){#profileMetadata[open],#profileLimits[open]{grid-template-columns:1fr}#profilesDialog{padding:20px}#profileFile::file-selector-button{display:block;margin-bottom:8px}}
  `;
  document.head.append(style);
  function element(tag, attrs={}, text='') {
    const node=document.createElement(tag);
    for(const [key,value] of Object.entries(attrs))node.setAttribute(key,value);
    if(text)node.textContent=text;
    return node;
  }
  function group(parent, id, title, nodes) {
    const details=element('details',{id});
    details.append(element('summary',{},title));
    for(const node of nodes)if(node)details.append(node);
    parent.append(details);return details;
  }
  function caption(id,text) {
    const label=$(id).closest('label');
    for(const node of [...label.childNodes])if(node.nodeType===Node.TEXT_NODE)node.remove();
    label.prepend(document.createTextNode(text));return label;
  }
  const point=$('#pointControls');
  point.querySelector('h3').textContent='По высоте · дополнительно';
  $('#pointProfileAdd').className='text-button';$('#pointProfileAdd').textContent='Добавить профиль';
  const pointFields=element('div',{id:'pointProfileFields',hidden:''});
  const altitude=caption('#analysisAltitude','Высота над уровнем моря, м');
  const delta=caption('#analysisDelta','Чувствительность температуры, K');
  const oldRow=altitude.parentElement;
  pointFields.append(altitude);
  const height=group(pointFields,'cloudHeightDetails','Оценить высоту облака',[delta,point.querySelector('.confirmation')]);
  height.querySelector('summary').after(element('p',{class:'hint'},'Приближённое сопоставление с профилем. Подтверждения относятся только к выбранной точке.'));
  pointFields.append($('#calculatePoint'));
  point.insertBefore(pointFields,$('#profilePointResult'));
  oldRow.remove();
  caption('#analysisProfile','Профиль атмосферы');

  const route=$('#tab-route');
  route.prepend(element('p',{id:'routeHint',class:'hint','aria-live':'polite'}));
  $('#drawRoute').innerHTML=icon('route')+'Добавить точки';
  $('#routePoints').closest('details').querySelector('summary').textContent='Ввести координаты / GeoJSON';
  const departure=caption('#departure','Отправление, UTC');
  const speed=caption('#speed','Скорость, км/ч');
  const step=caption('#routeStep','Расстояние между выборками, км');
  const oldSpeedRow=speed.parentElement;
  const timing=element('div',{id:'routeTimingFields',hidden:''});timing.append(departure,speed);
  const timingToggle=element('label',{class:'check'});
  timingToggle.innerHTML='<input id="routeTimingEnabled" type="checkbox">Учитывать время прохождения';
  const advanced=group(route,'routeOptions','Время прохождения и шаг',[timingToggle,timing,step]);
  advanced.append(element('p',{class:'micro'},'Время прохождения позволяет проверить возраст снимка, но не превращает его в прогноз.'));
  oldSpeedRow.remove();
  const routeProfile=caption('#routeProfile','Профиль атмосферы');
  const routeHeight=element('div',{id:'routeHeightFields',hidden:''});
  routeHeight.append(caption('#routeAltitude','Высота над уровнем моря, м'));
  const add=element('button',{id:'routeProfileAdd',class:'text-button',type:'button'},'Добавить профиль');
  const profileOptions=group(route,'routeProfileOptions','Температура на высоте · дополнительно',[routeProfile,add,routeHeight]);
  profileOptions.append(element('p',{class:'hint'},'Для графика снимка профиль не нужен. Температура воздуха берётся только из выбранного профиля.'));
  const calculate=$('#calculateRoute');calculate.textContent='Показать значения вдоль маршрута';
  route.append(advanced,profileOptions,calculate,element('p',{id:'routeError',class:'error-text',role:'alert',hidden:''}),$('#routeResult'));
  route.append(element('a',{href:help+'#route',target:'_blank',rel:'noopener',class:'text-button'},'Как читать маршрут'));

  const dialog=$('#profilesDialog');
  dialog.querySelector('.dialog-head h2').textContent='Профиль атмосферы';
  dialog.querySelector('.dialog-head p').textContent='Температура по высоте из радиозонда или модели. Для просмотра снимка не нужен.';
  const details=$('#profileImportDetails');details.querySelector('summary').textContent='Добавить файл профиля';
  const fileLabel=$('#profileFile').closest('label');
  fileLabel.classList.add('profile-file-choice');
  $('#profileFile').accept='.csv,.json';
  details.insertBefore(element('p',{id:'profileImportHint',class:'hint'},'Выберите CSV или JSON. Сведения из JSON подставятся автоматически.'),fileLabel);
  const templates=element('p',{class:'hint'});
  templates.innerHTML='<a href="'+help+'#profile" target="_blank" rel="noopener">Где взять данные и как подготовить файл</a> · <a href="/docs/profile-template.csv" download>Пустой CSV-шаблон</a>';
  fileLabel.after(templates);
  const source=caption('#profileSource','Источник профиля');
  const time=caption('#profileTime','Срок профиля, UTC');
  const lat=caption('#profileLat','Широта профиля, °');
  const lon=caption('#profileLon','Долгота профиля, °');
  const radius=caption('#profileRadius','Радиус применения, км');
  const hours=caption('#profileHours','Допуск времени, ч');
  const oldRows=new Set([source.parentElement,lat.parentElement,radius.parentElement]);
  const metadata=group(details,'profileMetadata','Проверить источник, срок и координаты',[source,time,lat,lon]);
  const limits=group(details,'profileLimits','Допуски применения',[radius,hours]);
  limits.append(element('p',{class:'micro'},'Радиус и время ограничивают применение профиля, но не гарантируют его репрезентативность.'));
  for(const row of oldRows)if(row!==details)row.remove();
  const preview=element('div',{id:'profilePreview',class:'profile-preview','aria-live':'polite',hidden:''});
  templates.after(preview);
  metadata.hidden=true;limits.hidden=true;
  for(const selector of ['#profileSource','#profileTime','#profileLat','#profileLon'])$(selector).required=true;
  $('#profileTime').step=1;
  $('#profileLat').min=-90;$('#profileLat').max=90;$('#profileLat').step='any';
  $('#profileLon').min=-180;$('#profileLon').max=180;$('#profileLon').step='any';
  $('#profileRadius').min=1;$('#profileRadius').max=500;$('#profileRadius').step='any';
  $('#profileHours').min=.1;$('#profileHours').max=12;$('#profileHours').step='any';
  const format=group(details,'profileFormat','Столбцы CSV',[]);
  format.append(element('p',{class:'micro'},'Обязательны height_m и temperature_c либо temperature_k. Дополнительно: pressure_hpa, specific_humidity_kg_kg, cloud_liquid_kg_kg, cloud_ice_kg_kg, u_ms, v_ms. Единицы и структура проверяются при добавлении.'));
  for(const node of [...details.querySelectorAll(':scope > p')])if(node.textContent.startsWith('CSV:'))node.remove();
  details.append(element('p',{id:'profileImportError',class:'error-text',role:'alert',hidden:''}),$('#importProfile'));
  $('#importProfile').disabled=true;$('#importProfile').textContent='Добавить и использовать';
  $('#profilesOpen').querySelector('span').textContent='Профили';
  for(const link of $$('a[href]')){
    const href=link.getAttribute('href');
    if(/^\/docs\/[A-Z_]+\.md(?:#.*)?$/.test(href))link.href='/docs/index.html#'+href.split('/').at(-1).split('.')[0].toLowerCase();
  }
  const firstHelp=$('#helpDialog a[href="/docs/index.html"]');
  if(firstHelp){firstHelp.href=help;firstHelp.textContent='Как работать со снимком, маршрутом и профилем';}
  syncProfileControls();syncRouteControls();
}

function inlineError(selector,message) {
  const node=$(selector);node.textContent=message||'';node.hidden=!message;
}

function syncProfileControls() {
  if(!$('#pointProfileFields'))return;
  const point=Boolean($('#analysisProfile').value),route=Boolean($('#routeProfile').value);
  $('#pointProfileFields').hidden=!point;
  $('#routeHeightFields').hidden=!route;
  $('#analysisAltitude').disabled=!point;$('#analysisDelta').disabled=!point;
  $('#routeAltitude').disabled=!route;
  $('#calculatePoint').disabled=!point;
  $('#analysisProfile').closest('label').hidden=!UI.profiles.length;
  $('#routeProfile').closest('label').hidden=!UI.profiles.length;
  const hasWindows=[7,9,10].every(ch=>UI.analysis?.pixel.channels.some(c=>Number(c.channel)===ch&&c.unit==='K'&&c.value!==null));
  $('#cloudHeightDetails').hidden=!point||!hasWindows;
  if(!point||!hasWindows){$('#cloudConfirmed').checked=false;$('#opaqueConfirmed').checked=false;}
  $('#cloudConfirmed').disabled=!point||!hasWindows;$('#opaqueConfirmed').disabled=!point||!hasWindows;
}

function syncRouteControls() {
  if(!$('#routeHint'))return;
  let count=0;try{count=routePoints().length;}catch{/* Незавершённый текст координат ещё не маршрут. */}
  const busy=Boolean(UI.routeBusy);
  $('#calculateRoute').disabled=!S.product||count<2||busy||Boolean(UI.waypointInvalid);
  $('#drawRoute').disabled=!S.product;
  $('#undoRoute').disabled=!count;$('#clearRoute').disabled=!count&&!$('#routePoints').value;
  $('#routeHint').textContent=!S.product?'Откройте снимок, затем добавьте точки маршрута.':count<2?'Добавьте как минимум две точки на карте или введите координаты.':`${count} точек маршрута. Перетаскивайте точки на карте или меняйте координаты ниже.`;
  const timed=$('#routeTimingEnabled').checked;
  $('#routeTimingFields').hidden=!timed;$('#departure').disabled=!timed;$('#speed').disabled=!timed;
}

function clearRange() {$('#displayMin').value='';$('#displayMax').value='';}

// viewBox изменяет координаты и растр; размеры подписей и маркеров — экранные.
function screenMapSymbols() {
  const map=$('#map'),matrix=map.getScreenCTM();if(!matrix)return;
  const k=Math.hypot(matrix.a,matrix.b);if(!Number.isFinite(k)||k<=0)return;
  for(const text of $$('#map text')){
    text.style.fontSize=(12/k)+'px';text.style.strokeWidth=(2.5/k)+'px';
    if(text.dataset.anchorX!==undefined){text.setAttribute('x',Number(text.dataset.anchorX)+10/k);text.setAttribute('y',Number(text.dataset.anchorY)-8/k);}
  }
  for(const circle of $$('#routeLayer circle[data-route-index]'))circle.setAttribute('r',6/k);
  for(const [i,circle] of $$('#pointLayer circle').entries()){
    circle.setAttribute('r',(i===0?7:2)/k);circle.setAttribute('stroke-width',2/k);
  }
  const box=map.getBoundingClientRect(),occupied=[];
  for(const text of $$('#labels text')){
    text.style.visibility='visible';const r=text.getBoundingClientRect();
    const outside=r.left<box.left+5||r.right>box.right-5||r.top<box.top+5||r.bottom>box.bottom-5;
    const overlaps=occupied.some(b=>r.left<b.right+8&&r.right>b.left-8&&r.top<b.bottom+4&&r.bottom>b.top-4);
    if(outside||overlaps)text.style.visibility='hidden';else occupied.push(r);
  }
}

function watchMapSymbols() {
  let pending=false;
  const update=()=>{if(!pending){pending=true;requestAnimationFrame(()=>{pending=false;screenMapSymbols();});}};
  new MutationObserver(update).observe($('#map'),{attributes:true,attributeFilter:['viewBox']});
  for(const id of ['#labels','#routeLayer','#pointLayer','#motionLayer'])new MutationObserver(update).observe($(id),{childList:true});
  new ResizeObserver(update).observe($('#map'));update();
}

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
  UI.routeRevision++;S.route=null;UI.routeContext=null;$('#routeResult').replaceChildren();
  if($('#routeError'))inlineError('#routeError','');syncRouteControls();
}

function invalidatePoint() {
  UI.pointSequence++;UI.lastPoint=null;UI.analysis=null;
  $('#analysisExport').hidden=true;$('#analysisExport').removeAttribute('href');
  $('#profilePointResult').replaceChildren();
  $('#pointLayer').replaceChildren();$('#pointControls').hidden=true;
  $('#pixel').innerHTML=`<div class="inspector-empty">${icon('probe')}<h3>Исследуйте облачность</h3><p>Выберите точку на карте.</p></div>`;
  $('#cloudConfirmed').checked=false;$('#opaqueConfirmed').checked=false;syncProfileControls();
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
  if(changed){clearRange();invalidateRoute();}
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
    clearRange();$('#product').value='channel';$('#channel').value=b.dataset.channel;productHint();requestBuild();
  });
}

const TASKS=[
  {id:'micro24',icon:'cloud',name:'Облачность',hint:'Фаза и прозрачность'},
  {id:'channel',icon:'thermometer',name:'Оконный ИК',hint:'Канал 9 · температура при калибровке'},
  {id:'night',icon:'fog',name:'Низкие облака',hint:'Ночная микрофизика'},
  {id:'difference',icon:'layers',name:'Разности',hint:'Оконный контраст'},
  {id:'phase',icon:'snow',name:'Признаки фазы',hint:'Исследовательские правила'},
  {id:'motion',icon:'satellite',name:'Динамика',hint:'Два срока'},
];

function tasks() {
  $('#taskGrid').innerHTML=TASKS.map(t=>`<button class="task-card" data-task="${t.id}">${icon(t.icon)}<span>${t.name}<small>${t.hint}</small></span></button>`).join('');
  $$('#taskGrid button').forEach(b=>b.onclick=()=>{
    if(b.dataset.task==='motion') {$('#motionOpen').click();return;}
    clearRange();$('#product').value=b.dataset.task;
    if(b.dataset.task==='channel')$('#channel').value='9';
    productHint();requestBuild();
  });
}

productHint = function() {
  original.productHint();drawChannelChips();
  $$('#taskGrid button').forEach(b=>b.classList.toggle('active',b.dataset.task===$('#product').value));
  const guide=UI.guides[$('#product').value];
  if(guide)$('#productHint').textContent=guide.purpose;
  const have=new Set((S.scene?.channels||[]).map(c=>c.channel));
  for(const button of $$('#taskGrid button')){
    const id=button.dataset.task,product=S.registry.products.find(p=>p.id===id);
    const required=id==='channel'?[9]:(product?.channels||[]);
    const missing=required.filter(ch=>!have.has(ch));
    button.disabled=id==='motion'?S.scenes.length<2:!S.scene||missing.length>0;
    button.title=missing.length?'Нужны каналы: '+missing.join(', '):!S.scene?'Сначала откройте снимок':'';
    button.setAttribute('aria-pressed',String(id===$('#product').value));
  }
  const item=S.registry.products.find(p=>p.id===$('#product').value);
  const needed=item?.id==='channel'?[Number($('#channel').value)]:(item?.channels||[]);
  const missing=needed.filter(ch=>!have.has(ch));
  if(S.scene&&missing.length)$('#productHint').textContent='Для этого продукта скачайте каналы '+missing.join(', ')+'. Откройте «Каналы и файлы» выбранного срока.';
  const unit=S.product?.product==='channel'&&Number(S.product.request.channel)===Number($('#channel').value)?(S.product.legend.units==='K'?'°C':S.product.legend.units):'исходные единицы';
  for(const [id,title] of [['#displayMin','От'],['#displayMax','До']]){
    const label=$(id).closest('label');if(label.firstChild.nodeType===Node.TEXT_NODE)label.firstChild.textContent=title+', '+unit;
  }
  syncRouteControls();
};

function buildRequest() {
  if(!S.scene)return null;
  const body={scene:S.scene.id,product:$('#product').value,channel:Number($('#channel').value),preset:$('#preset').value,width:1000};
  if(body.product==='channel'){
    const offset=S.product?.product==='channel'&&Number(S.product.request.channel)===body.channel&&S.product.legend.units==='K'?273.15:0;
    if($('#displayMin').value!=='')body.display_min=Number($('#displayMin').value)+offset;
    if($('#displayMax').value!=='')body.display_max=Number($('#displayMax').value)+offset;
    if((body.display_min!==undefined&&!Number.isFinite(body.display_min))||(body.display_max!==undefined&&!Number.isFinite(body.display_max)))throw Error('Укажите конечные границы цветовой шкалы.');
    if(body.display_min!==undefined&&body.display_max!==undefined&&body.display_min>=body.display_max)throw Error('Начало шкалы должно быть меньше конца.');
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
  let guide=interpretedGuide(l.product);if(l.product==='channel'&&l.units!=='K')guide={title:'Значения канала · DN',purpose:'Числа из файла, не температура. Цвет помогает сравнивать сигнал и различать структуру.',swatches:[]};const temperature=l.units==='K'&&l.product==='channel';
  const value=v=>temperature&&v!==null?v-273.15:v;
  const unit=temperature?'°C':l.units;
  let html=`<h3>${escape(guide.title||l.title)}</h3><p class="hint">${escape(guide.purpose||l.meaning||'')}</p>`;
  if(l.status==='assumed')html+=`<div class="caveat">${icon('tune')}<span>DN = K — принятое допущение</span></div>`;
  html+=legendSwatches(guide);
  let miniature='';
  if(l.palette) {
    const grad=l.palette.map(c=>'rgb('+c.join(',')+')').join(',');
    const bar=`<div class="colorbar" style="background:linear-gradient(to right,${grad})"></div><div class="scale-labels"><span>${num(value(l.display_min))} ${escape(unit)}</span><span>${num(value(l.display_max))} ${escape(unit)}</span></div>`;
    html+='<h3>Цветовая шкала</h3>'+bar;miniature=bar;
    if(l.product==='channel')html+='<p class="hint">За пределами шкалы — крайние цвета. Исходные значения сохранены.</p><div class="row"><button id="legendAutoRange" class="text-button">Автоконтраст</button><button id="legendFullRange" class="text-button">Весь диапазон</button></div>';
  }
  if(l.stats)html+=`<details><summary>Диапазон данных: ${num(value(l.stats.min))}…${num(value(l.stats.max))} ${escape(unit)}</summary><p class="hint">Минимум и максимум валидных значений на расчётной сетке. Это не границы цветовой шкалы. Автоконтраст DN использует 2-й и 98-й процентили.</p></details>`;
  if(l.classes){
    html+=l.classes.map(c=>`<div class="legend-item"><span class="swatch" style="background:${c.color}"></span><span>${escape(c.name)}</span></div>`).join('');
    miniature=`<div class="mini-swatches">${l.classes.filter(c=>c.value>0).slice(0,3).map(c=>`<span><i style="background:${c.color}"></i>${escape(c.name.split(':')[0])}</span>`).join('')}</div>`;
  }
  if(guide.swatches?.length)miniature=`<div class="mini-swatches">${guide.swatches.map(c=>`<span><i style="background:${c.color}"></i>${escape(c.title.split(' / ')[0])}</span>`).join('')}</div>`;
  html+='<details><summary>Компоненты, шкалы и качество</summary>';
  if(l.components)html+=l.components.map(c=>`<div class="component" style="border-color:${c.component==='R'?'#bf6576':c.component==='G'?'#43977e':'#5587c7'}"><b>${c.component}</b> <code>${escape(c.formula)}</code><small>${c.min}…${c.max} K · γ ${c.gamma}</small><small>В области: ${num(c.stats.min)}…${num(c.stats.max)} K</small></div>`).join('');
  html+=`<p class="micro">${escape(l.data_stats_scope||'')}<br>${escape(l.nodata||'')}<br>Калибровка: ${escape(sourceCal[l.status]||l.status)}.</p><pre>${escape(JSON.stringify({flags:l.quality_flags,version:l.version},null,2))}</pre></details><a class="export-link" href="/docs/QUICKSTART.html#signal" target="_blank" rel="noopener">Как читать значения и цвета</a>`;
  $('#legend').innerHTML=html;
  if($('#legendAutoRange'))$('#legendAutoRange').onclick=()=>{clearRange();requestBuild();};
  if($('#legendFullRange')){
    $('#legendFullRange').disabled=!Number.isFinite(l.stats?.min)||!Number.isFinite(l.stats?.max);
    $('#legendFullRange').onclick=()=>{$('#displayMin').value=value(l.stats.min);$('#displayMax').value=value(Math.max(l.stats.max,l.stats.min+1));requestBuild();};
  }
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
    altitude_m:!$('#analysisProfile').value||$('#analysisAltitude').value===''?null:Number($('#analysisAltitude').value),
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
  const featured=['t'+(S.product?.request.channel||9),'t9','d97','d109','d94'].filter((v,i,a)=>a.indexOf(v)===i);
  if(a.metrics.length)html+=`<div class="metric-grid">${featured.map(id=>a.metrics.find(m=>m.id===id)).filter(Boolean).map(m=>`<div class="metric ${m.id==='t9'?'primary-metric':''}" tabindex="0" data-tip="${escape(m.explanation)}"><small>${escape(m.label)}</small><b>${num(m.value)} <small>${m.units}</small></b></div>`).join('')}</div>`;
  if(!a.metrics.length){const ch=p.channels.find(c=>Number(c.channel)===Number(S.product?.request.channel))||p.channels.find(c=>c.value!==null);if(ch)html+=`<div class="metric"><small>Канал ${ch.channel} · значение в точке</small><b>${num(ch.value)} <small>${escape(ch.unit)}</small></b></div>`;}
  html+=a.cards.map(c=>`<article class="evidence-card ${escape(c.tone)}"><div class="evidence-card-head">${icon(c.icon)}<h3>${escape(c.title)}</h3></div><p>${escape(c.meaning)}</p>${c.evidence.length?`<details><summary>На чём основано</summary>${c.evidence.map(t=>`<div class="evidence-row">${escape(t)}</div>`).join('')}<p class="micro">Пороговая гипотеза МСУ-ГС/А; не калиброванная вероятность.</p></details>`:''}<div class="action">${escape(c.action)}</div></article>`).join('');
  html+=`<details><summary>Все каналы и исходные значения</summary><table><thead><tr><th>Канал</th><th>Tя / DN</th><th>Единицы</th></tr></thead><tbody>${p.channels.map(c=>`<tr><td>${c.channel}<small>${channelMeta(c.channel)?.wavelength_um} мкм</small></td><td>${num(c.unit==='K'&&c.value!==null?c.value-273.15:c.value)}</td><td>${c.unit==='K'?'°C':escape(c.unit)}</td></tr>`).join('')}</tbody></table><p class="micro">Солнце: ${num(p.sun_elevation)}° · флаги продукта: ${p.quality_flags}. Спектральные разности вычислены из исходных пикселей.</p></details>`;
  $('#pixel').innerHTML=html;$('#pointControls').hidden=false;syncProfileControls();
  $('#analysisExport').href='/analysis-export/'+r.id;$('#analysisExport').hidden=false;
  drawProfilePoint(r.profile_result);
}

function drawProfilePoint(r) {
  if(!r){$('#profilePointResult').innerHTML='<p class="hint">Для просмотра значений снимка профиль не нужен. Добавьте его только для температуры воздуха и расчётов по высоте.</p>';return;}
  const src=r.profile;
  let html=`<span class="profile-source">${escape(src.source)} · ${escape(src.valid_time.replace('T',' ').replace('Z',' UTC'))}<br>${num(src.distance_km,0)} км до профиля · расхождение ${num(src.time_gap_hours)} ч</span>`;
  if(!src.applicable){$('#profilePointResult').innerHTML=html+`<div class="caveat">${icon('info')}<span>${src.reasons.map(escape).join('<br>')}</span></div>`;return;}
  if(r.profile_plot)html+=profileChart(r);
  const h=r.height;
  if(!$('#cloudHeightDetails').hidden&&($('#cloudHeightDetails').open||h.candidate_heights_m)){
  html+=`<section class="profile-result"><h3>${icon('height')} ${escape(h.title)}</h3>`;
  if(h.candidate_heights_m){
    html+=`<div class="profile-value">${h.candidate_heights_m.length?h.candidate_heights_m.map(v=>num(v/1000,2)).join(' / '):'—'} <small>км AMSL</small></div>`;
    if(h.ambiguous)html+='<p class="hint">Неоднозначное соответствие T(z); единственная высота не выбрана.</p>';
    html+=`<p class="micro">При Tя ± ${num(h.delta_k)} K: ${(h.sensitivity_ranges_m||[]).map(x=>num(x[0]/1000,2)+'–'+num(x[1]/1000,2)+' км').join('; ')||'нет пересечения'}. Диапазон чувствительности, не доверительный интервал.</p>`;
    html+=`<details><summary>Допущения высоты</summary><p class="micro">${escape(h.method)}. Атмосферная поправка не выполнена. Облачность и непрозрачность подтверждены оператором только для этой точки.</p></details>`;
  }
  html+='</section>';
  }
  const l=r.layer;
  html+=`<section class="profile-result"><h3>На ${num(l.altitude_m??Number($('#analysisAltitude').value),0)} м</h3><div class="layer-state ${l.status==='supercooled'?'supercooled':''}">${escape(l.title)}</div>`;
  if(l.temperature_c!==undefined)html+=`<div class="metric-grid"><div class="metric"><small>Температура воздуха</small><b>${num(l.temperature_c)} <small>°C</small></b></div>${l.liquid_water_g_m3!==undefined?`<div class="metric"><small>Жидкая вода</small><b>${num(l.liquid_water_g_m3,3)} <small>г/м³</small></b></div>`:''}</div>`;
  if(l.cloud_liquid_kg_kg!==undefined)html+=`<details><summary>Основание оценки слоя</summary><p class="micro">qₗ = ${num(l.cloud_liquid_kg_kg*1e6,2)} мг/кг. Порог выделения: ${num(l.liquid_floor_kg_kg*1e6,2)} мг/кг. Плотность: ${escape(l.density_method||'не определена')}. Оценка не задаёт интенсивность обледенения, размер капель или безопасность полёта.</p></details>`;
  html+='</section>';
  if(r.column)html+=`<section class="profile-result"><h3>В заданном столбе профиля</h3><div class="metric-grid"><div class="metric"><small>Водяной пар</small><b>${num(r.column.vapor_mm)} <small>мм</small></b></div>${r.column.ivt_kg_m_s!==undefined?`<div class="metric"><small>Перенос влаги</small><b>${num(r.column.ivt_kg_m_s,0)} <small>кг/(м·с)</small></b></div>`:''}</div><p class="micro">${r.column.pressure_range_pa.map(v=>num(v/100,0)).join('–')} гПа · из профиля, не из цвета снимка.</p></section>`;
  $('#profilePointResult').innerHTML=html;
}

function profileChart(result) {
  const d=result.profile_plot,z=d.height_m,t=d.temperature_c;
  let lo=Math.floor(Math.min(...t)/10)*10,hi=Math.ceil(Math.max(...t)/10)*10;if(lo===hi)hi=lo+10;
  const bottom=z[0],top=z[z.length-1];
  const x=v=>40+(v-lo)/(hi-lo)*230,y=v=>190-(v-bottom)/(top-bottom)*166;
  const path=t.map((v,i)=>x(v)+','+y(z[i])).join(' ');
  const layer=result.layer;
  let guides='';
  for(let v=lo;v<=hi;v+=10)guides+=`<line x1="${x(v)}" y1="24" x2="${x(v)}" y2="190" stroke="#e0e6ef"/><text x="${x(v)}" y="207" text-anchor="middle" fill="#65768b" font-size="10">${v}</text>`;
  for(let j=0;j<=3;j++){const v=bottom+j*(top-bottom)/3;guides+=`<line x1="40" y1="${y(v)}" x2="270" y2="${y(v)}" stroke="#e0e6ef"/><text x="35" y="${y(v)+3}" text-anchor="end" fill="#65768b" font-size="10">${num(v/1000,1)}</text>`;}
  if(layer.temperature_c!==undefined)guides+=`<line x1="40" y1="${y(layer.altitude_m)}" x2="270" y2="${y(layer.altitude_m)}" stroke="#2d8e80" stroke-dasharray="4 3"/><circle cx="${x(layer.temperature_c)}" cy="${y(layer.altitude_m)}" r="4" fill="#2d8e80"/>`;
  return `<svg class="profile-chart" viewBox="0 0 300 224" role="img" aria-label="Температурный профиль: высота в километрах, температура в градусах Цельсия"><text x="40" y="13" fill="#4a627c" font-size="11">T(z) · ${escape(result.profile.source.slice(0,24))}</text>${guides}<polyline points="${path}" fill="none" stroke="#345dca" stroke-width="2"/><text x="250" y="220" fill="#65768b" font-size="10">°C</text><text x="4" y="14" fill="#65768b" font-size="10">км</text></svg>`;
}

async function refreshProfiles(selectId) {
  const response=await api('/api/profiles');UI.profiles=response.profiles;
  for(const selector of ['#analysisProfile','#routeProfile']){
    const old=$(selector).value;
    $(selector).innerHTML='<option value="">Только снимок — без профиля</option>'+UI.profiles.map(p=>`<option value="${escape(p.id)}">${escape(p.source+' · '+p.valid_time.slice(0,10)+' '+p.valid_time.slice(11,16)+' UTC')}</option>`).join('');
    if(UI.profiles.some(p=>p.id===old))$(selector).value=old;
  }
  $('#profileList').innerHTML=UI.profiles.map(p=>`<article class="profile-list-item"><h3>${escape(p.source)}</h3><p>${escape(p.valid_time.replace('T',' ').replace('Z',' UTC'))}</p><p>${num(p.lat,2)}°, ${num(p.lon,2)}° · ${p.radius_km} км / ${p.max_hours} ч</p><button class="text-button" data-profile="${escape(p.id)}">Использовать ${UI.profileTarget==='route'?'для маршрута':'для точки'}</button></article>`).join('');
  $$('#profileList button').forEach(b=>b.onclick=()=>useProfile(b.dataset.profile).catch(e=>toast(e.message)));
  syncProfileControls();
  if(selectId)await useProfile(selectId);
}

async function useProfile(id) {
  const route=UI.profileTarget==='route';
  $(route?'#routeProfile':'#analysisProfile').value=id;
  invalidateRoute();syncProfileControls();$('#profilesDialog').close();
  if(route){$('#routeProfileOptions').open=true;tab('route');$('#calculateRoute').focus();}
  else if(UI.lastPoint)await inspectAt(UI.lastPoint.x,UI.lastPoint.y,true);
}

async function showProfiles(target) {
  UI.profileTarget=target||(!$('#tab-route').hidden?'route':'point');
  await refreshProfiles();open('#profilesDialog');
  if(!UI.profiles.length)$('#profileImportDetails').open=true;
}

function profileMetadata() {
  const local=$('#profileTime').value;
  return {source:$('#profileSource').value.trim(),lat:$('#profileLat').value,lon:$('#profileLon').value,
    valid_time:local?local+(local.length===16?':00Z':'Z'):'',radius_km:$('#profileRadius').value,max_hours:$('#profileHours').value};
}

function profileReady() {
  const selectors=['#profileSource','#profileTime','#profileLat','#profileLon','#profileRadius','#profileHours'];
  const ready=Boolean(UI.profileText)&&selectors.every(id=>$(id).value.trim()!==''&&$(id).checkValidity());
  $('#importProfile').disabled=!ready||Boolean(UI.profileImportBusy);
  for(const id of selectors)$(id).setAttribute('aria-invalid',String(Boolean(UI.profileText)&&(!$(id).value.trim()||!$(id).checkValidity())));
  return ready;
}

async function profileFileChanged() {
  const sequence=(UI.profileFileSequence||0)+1;UI.profileFileSequence=sequence;
  UI.profileText='';$('#profileFileName').textContent='';$('#profilePreview').replaceChildren();$('#profilePreview').hidden=true;
  $('#profileMetadata').hidden=true;$('#profileLimits').hidden=true;
  inlineError('#profileImportError','');
  for(const id of ['#profileSource','#profileLat','#profileLon','#profileTime'])$(id).value='';
  $('#profileRadius').value=100;$('#profileHours').value=3;profileReady();
  const file=$('#profileFile').files[0];if(!file)return;
  if(file.size>1_000_000){inlineError('#profileImportError','Файл больше 1 МБ. Выберите один вертикальный профиль.');return;}
  try{
    const text=(await file.text()).replace(/^\uFEFF/,'');if(sequence!==UI.profileFileSequence)return;
    if(!text.trim())throw Error('Файл пуст. Выберите CSV или JSON с уровнями температуры.');
    let summary='CSV: источник, срок и координаты укажите ниже.';
    if(text.trim().startsWith('{')){
      const p=JSON.parse(text);
      if(!p||typeof p!=='object'||Array.isArray(p))throw Error('Нужен JSON-объект профиля.');
      for(const [field,selector] of [['source','#profileSource'],['lat','#profileLat'],['lon','#profileLon'],['radius_km','#profileRadius'],['max_hours','#profileHours']])if(p[field]!==undefined&&p[field]!==null)$(selector).value=p[field];
      if(p.valid_time){const stamp=String(p.valid_time).trim();if(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(stamp)){const date=new Date(stamp);if(Number.isFinite(date.getTime()))$('#profileTime').value=date.toISOString().slice(0,19);}}
      if(!Array.isArray(p.height)||!Array.isArray(p.temperature)||p.height.length<2||p.height.length!==p.temperature.length)throw Error('В JSON нужны height и temperature с одинаковым числом уровней, не менее двух.');
      summary='JSON: '+p.height.length+' уровней. '+(p.cloud_liquid?'Есть жидкий конденсат.':'Жидкий конденсат не задан — условия переохлаждённой воды не оцениваются.');
    }else{
      const header=text.trim().split(/\r?\n/)[0].split(/[;,]/).map(x=>x.trim());
      if(!header.includes('height_m')||!header.some(x=>x==='temperature_c'||x==='temperature_k'))throw Error('CSV: нужны столбцы height_m и temperature_c либо temperature_k. Формат описан по ссылке над полями.');
    }
    UI.profileText=text;$('#profileFileName').textContent=file.name;
    const missing=['#profileSource','#profileTime','#profileLat','#profileLon'].some(id=>!$(id).value.trim());
    $('#profilePreview').textContent=summary+(missing?' Заполните недостающие сведения.':' Источник: '+$('#profileSource').value+'; срок: '+$('#profileTime').value.replace('T',' ')+' UTC; координаты: '+$('#profileLat').value+'°, '+$('#profileLon').value+'°.');
    $('#profilePreview').hidden=false;$('#profileMetadata').hidden=false;$('#profileLimits').hidden=false;$('#profileMetadata').open=missing;
    $('#profileImportHint').textContent='Проверьте сведения. Структура и единицы будут проверены при добавлении.';profileReady();
  }catch(error){if(sequence===UI.profileFileSequence){UI.profileText='';inlineError('#profileImportError',error.message);profileReady();}}
}

async function importProfile() {
  if(!profileReady()){
    $('#profileMetadata').open=true;inlineError('#profileImportError','Выберите файл и заполните источник, срок и координаты профиля.');return;
  }
  const sequence=UI.profileFileSequence;
  const data={text:UI.profileText,metadata:profileMetadata()};
  const target=UI.profileTarget,fingerprint=JSON.stringify(data.metadata);
  UI.profileImportBusy=true;profileReady();inlineError('#profileImportError','');
  try{
    const result=await api('/api/profiles/import',data);
    if(sequence!==UI.profileFileSequence||fingerprint!==JSON.stringify(profileMetadata())||target!==UI.profileTarget){await refreshProfiles();return;}
    await refreshProfiles(result.id);$('#profileImportDetails').open=false;toast('Профиль добавлен и выбран '+(UI.profileTarget==='route'?'для маршрута':'для точки'));
  }catch(error){if(sequence===UI.profileFileSequence){inlineError('#profileImportError',error.message);$('#profileMetadata').open=true;}}
  finally{UI.profileImportBusy=false;profileReady();}
}

function setDrawing(value) {
  S.drawing=value;$('#drawingPill').hidden=!value;
  $('#drawRoute').classList.toggle('draw-active',value);$('#drawRoute').innerHTML=icon('route')+(value?'Добавление точек':'Добавить точки');
  if(value){tab('route');if(innerWidth<720)$('#inspector').hidden=true;}
}

async function redrawRouteDraft() {
  invalidateRoute();
  let points=[];try{points=routePoints();}catch{
    $('#routeLayer').replaceChildren();$('#routeWaypointList').replaceChildren();return;
  }
  $('#routeWaypointList').innerHTML=points.map((p,i)=>`<div class="waypoint"><span class="waypoint-number">${i+1}</span><input aria-label="Долгота и широта точки ${i+1}" data-index="${i}" value="${num(p[0],3).replace(',','.')}, ${num(p[1],3).replace(',','.')}" data-tip="Долгота, широта. Enter — применить."><button class="icon-button" data-remove="${i}" aria-label="Удалить точку ${i+1}">${icon('delete')}</button></div>`).join('');
  $$('#routeWaypointList input').forEach(input=>{
    const commit=()=>{const numbers=input.value.trim().split(/[,;\s]+/).map(Number);if(numbers.length!==2||numbers.some(v=>!Number.isFinite(v))||Math.abs(numbers[0])>180||Math.abs(numbers[1])>90){toast('Введите долготу и широту через запятую.');return;}UI.waypointInvalid=false;points[Number(input.dataset.index)]=numbers;$('#routePoints').value=points.map(p=>p.join(', ')).join('\n');redrawRouteDraft().catch(e=>toast(e.message));};
    input.oninput=()=>{UI.waypointInvalid=true;invalidateRoute();$('#calculateRoute').disabled=true;};
    input.onchange=commit;input.onkeydown=e=>{if(e.key==='Enter')input.blur();};
  });
  $$('#routeWaypointList [data-remove]').forEach(b=>b.onclick=()=>{UI.waypointInvalid=false;points.splice(Number(b.dataset.remove),1);$('#routePoints').value=points.map(p=>p.join(', ')).join('\n');redrawRouteDraft().catch(e=>toast(e.message));});
  if(!S.product){$('#routeLayer').replaceChildren();return;}
  const seq=++UI.routeRevision;
  if(!points.length){$('#routeLayer').replaceChildren();return;}
  const r=await api('/api/project-points',{product:S.product.id,points});if(seq!==UI.routeRevision)return;
  const lines=[];let current=[];
  for(const p of (r.line||r.points)){if(!p.every(Number.isFinite)){if(current.length)lines.push(current);current=[];continue;}if(current.length&&Math.abs(current.at(-1)[0]-p[0])>S.map.grid.width*.6){lines.push(current);current=[];}current.push(p);}
  if(current.length)lines.push(current);
  const markers=r.points.flatMap((p,i)=>p.every(Number.isFinite)?[svgEl('circle',{cx:p[0],cy:p[1],r:6,class:'route-point','data-route-index':i}),(()=>{const t=svgEl('text',{x:p[0]+10,y:p[1]-7,'data-anchor-x':p[0],'data-anchor-y':p[1]});t.textContent=i+1;return t;})()]:[]);
  $('#routeLayer').replaceChildren(...lines.filter(l=>l.length>1).map(l=>polyline(l,'route-line')),...markers);
  enableWaypointDrag();screenMapSymbols();syncRouteControls();
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

function routeChannel(r, requested) {
  const channels=Object.keys(r.units||{}).sort((a,b)=>Number(a)-Number(b));
  const preferred=String(requested??(S.product?.product==='channel'?S.product.request.channel:9));
  return channels.includes(preferred)?preferred:channels.includes('9')?'9':channels[0];
}

function routeValue(row, ch, units) {
  const value=row.channels?.[ch];
  if(value===null||value===undefined||!Number.isFinite(Number(value)))return null;
  return units[ch]?.unit==='K'?Number(value)-273.15:Number(value);
}

function routeUnit(r,ch) {return r.units[ch]?.unit==='K'?'°C':r.units[ch]?.unit||'DN';}

function routeLayerText(row) {
  const result=row.profile_result;
  if(!result)return 'Нет профильных данных';
  if(result.profile?.applicable===false)return 'Вне профиля';
  const layer=result.layer;
  if(!layer)return 'Нет профильных данных';
  if(layer.status==='supercooled')return 'Переохлаждённая жидкая вода';
  if(layer.temperature_c!==undefined)return num(layer.temperature_c)+' °C · '+layer.title;
  return layer.title||'Нет данных в слое';
}

function renderRoute(r, requested) {
  const ch=routeChannel(r,requested);if(ch===undefined){$('#routeResult').innerHTML='<p class="hint">Нет доступных каналов.</p>';return;}
  const unit=routeUnit(r,ch),channels=Object.keys(r.units).sort((a,b)=>Number(a)-Number(b));
  const hasProfile=Boolean(UI.routeContext?.profile),timing=Boolean(UI.routeContext?.timed);
  const missing=r.samples.filter(row=>routeValue(row,ch,r.units)===null).length;
  let html=`<div class="section-heading"><h3>${num(r.length_km,0)} км</h3><span class="micro">${r.samples.length} выборок</span></div><p class="hint">Снимок: ${escape(r.time.replace('T',' ').replace('Z',' UTC'))}.</p>`;
  if(channels.length>1)html+=`<label>Канал на графике и в таблице<select id="routeChannel">${channels.map(c=>`<option value="${escape(c)}" ${c===ch?'selected':''}>Канал ${escape(c)} · ${escape(routeUnit(r,c))}</option>`).join('')}</select></label>`;
  if(unit==='DN')html+='<p class="hint">Изменение сигнала вдоль маршрута. Это не температура воздуха и не высота облаков.</p>';
  else html+='<p class="hint">Яркостная температура канала, не температура воздуха на высоте маршрута.</p>';
  if(r.units[ch].calibration==='assumed')html+='<p class="caveat">DN = K — исследовательское допущение, не подтверждённая калибровка.</p>';
  html+=routeChart(r,ch);
  if(missing)html+=`<p class="caveat">Нет данных канала ${escape(ch)} в ${missing} из ${r.samples.length} выборок. Разрывы графика не заполнены.</p>`;
  if(timing){const old=r.samples.filter(x=>x.observation_age_minutes>60).length,later=r.samples.filter(x=>x.observation_age_minutes<0).length;html+=`<p class="hint">К прохождению: ${old} выборок со снимком старше 60 мин; ${later} — со снимком позже прохождения. Это наблюдение, не прогноз.</p>`;}
  html+=`<details><summary>Сохранить результат</summary><div class="row wrap"><a href="/route-export/${r.id}/csv" download>Таблица CSV</a><a href="/route-export/${r.id}/geojson" download>Точки GeoJSON</a><a href="/route-export/${r.id}/json" download>Полный расчёт JSON</a></div><p class="micro">В экспорте сохранены исходные единицы каналов, сроки, пропуски и происхождение данных. На экране K показаны в °C.</p></details>`;
  html+=`<div class="route-table"><table><thead><tr><th>Расстояние</th><th>Канал ${escape(ch)}, ${escape(unit)}</th>${timing?'<th>Прохождение UTC</th>':''}${hasProfile?'<th>По профилю</th>':''}</tr></thead><tbody>`;
  html+=r.samples.map((row,i)=>`<tr tabindex="0" data-sample="${i}" aria-label="Исследовать точку на ${num(row.distance_km,0)} км"><td>${num(row.distance_km,0)} км</td><td>${routeValue(row,ch,r.units)===null?'Нет данных':num(routeValue(row,ch,r.units))}</td>${timing?`<td>${escape(row.eta?.slice(11,16)||'Не задано')}<small>${escape(row.status||'')}</small></td>`:''}${hasProfile?`<td>${escape(routeLayerText(row))}</td>`:''}</tr>`).join('');
  html+='</tbody></table></div><p class="micro">Нажмите строку, чтобы открыть эту точку на снимке.</p>';
  $('#routeResult').innerHTML=html;
  if($('#routeChannel'))$('#routeChannel').onchange=()=>renderRoute(r,$('#routeChannel').value);
  $$('#routeResult [data-sample]').forEach(row=>{const openPoint=()=>{const xy=r.map_points[Number(row.dataset.sample)];if(xy?.every(Number.isFinite))inspectAt(...xy).catch(e=>toast(e.message));};row.onclick=openPoint;row.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openPoint();}};});
}

calculateRoute = async function() {
  invalidateRoute();
  if(!S.product){inlineError('#routeError','Откройте снимок для маршрута.');return;}
  const product=S.product.id,rev=UI.routeRevision;
  UI.routeBusy=true;syncRouteControls();$('#calculateRoute').textContent='Выборка значений…';
  try{
    const points=routePoints();if(points.length<2)throw Error('Добавьте как минимум две точки маршрута.');
    const timed=$('#routeTimingEnabled').checked,profile=$('#routeProfile').value;
    const step=Number($('#routeStep').value),speed=Number($('#speed').value);
    if(!Number.isFinite(step)||step<1||step>500)throw Error('Шаг выборки: от 1 до 500 км.');
    if(timed&&(!Number.isFinite(speed)||speed<=0||speed>2000))throw Error('Скорость: больше 0 и не более 2000 км/ч.');
    if(timed&&!$('#departure').value)throw Error('Укажите время отправления UTC или отключите учёт времени прохождения.');
    const altitude=profile&&$('#routeAltitude').value!==''?Number($('#routeAltitude').value):null;
    if(profile&&(altitude===null||!Number.isFinite(altitude)||altitude< -500||altitude>30000))throw Error('Укажите высоту от −500 до 30 000 м над уровнем моря.');
    const value=$('#departure').value;
    const data={product,points,step_km:step,speed_kmh:timed?speed:300,
      departure:timed?value+(value.length===16?':00Z':'Z'):'',profile_id:profile||undefined,altitude_m:altitude};
    const result=await api('/api/route',data);
    if(S.product?.id!==product||rev!==UI.routeRevision)return;
    S.route=result;UI.routeContext={timed,profile:Boolean(profile)};setDrawing(false);renderRoute(result);
  }catch(error){if(rev===UI.routeRevision)inlineError('#routeError',error.message);}
  finally{UI.routeBusy=false;$('#calculateRoute').textContent='Показать значения вдоль маршрута';syncRouteControls();}
};

routeChart=function(r,requested){
  const ch=routeChannel(r,requested),unit=routeUnit(r,ch);
  const values=r.samples.map(row=>routeValue(row,ch,r.units)),valid=values.filter(v=>v!==null);
  if(!valid.length)return '<p class="hint">В этом канале нет данных вдоль маршрута.</p>';
  const lo=Math.min(...valid),hi=Math.max(...valid),span=hi-lo||1;
  const x=row=>42+(r.length_km>0?row.distance_km/r.length_km:0)*240;
  const y=value=>138-(value-lo)/span*100;
  const paths=[];let path=[];
  values.forEach((v,i)=>{if(v===null){if(path.length)paths.push(path);path=[];}else path.push([x(r.samples[i]),y(v)]);});
  if(path.length)paths.push(path);
  return `<svg viewBox="0 0 300 170" role="img" aria-label="Значения канала ${escape(ch)} вдоль маршрута, ${escape(unit)}"><text x="42" y="18" fill="#476382" font-size="11">Канал ${escape(ch)} · ${escape(unit)}</text><line x1="42" x2="282" y1="138" y2="138" stroke="#ccd4df"/>${paths.map(p=>p.length>1?`<polyline points="${p.map(v=>v.join(',')).join(' ')}" fill="none" stroke="#137b77" stroke-width="2"/>`:`<circle cx="${p[0][0]}" cy="${p[0][1]}" r="2" fill="#137b77"/>`).join('')}<text x="34" y="42" text-anchor="end" fill="#476382" font-size="10">${num(hi)}</text><text x="34" y="141" text-anchor="end" fill="#476382" font-size="10">${num(lo)}</text><text x="42" y="158" fill="#476382" font-size="10">0 км</text><text x="282" y="158" text-anchor="end" fill="#476382" font-size="10">${num(r.length_km,0)} км</text></svg>`;
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
    const cal=S.product?.calibration_status||r.calibration?.mode;
    $('#calibrationState').textContent=displayCal[cal]||'DN';
    $('#calibrationOpen').dataset.tip=sourceCal[cal]||'Единицы каналов';
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
  workflowLayout();original.registerEvents();watchMapSymbols();
  bind('#catalogRail',()=>left('catalog',false));bind('#dateOpen',()=>left('catalog'));
  bind('#productRail',()=>left('product',false));bind('#productQuick',()=>left('product'));
  bind('#closeLeft',()=>{left(UI.leftMode,false);});
  bind('#routeRail',()=>{tab('route');});
  bind('#closeInspector',()=>{$('#inspector').hidden=true;$('#routeRail').classList.remove('active');});
  bind('#legendOpen',()=>tab('legend'));bind('#miniLegend',()=>tab('legend'));
  bind('#profilesOpen',()=>showProfiles());bind('#pointProfileAdd',()=>showProfiles('point'));bind('#routeProfileAdd',()=>showProfiles('route'));
  bind('#importProfile',importProfile);
  $('#profileFile').onchange=()=>profileFileChanged().catch(e=>inlineError('#profileImportError',e.message));
  for(const id of ['#profileSource','#profileLat','#profileLon','#profileTime','#profileRadius','#profileHours'])$(id).addEventListener('input',profileReady);
  $('#routeTimingEnabled').onchange=()=>{invalidateRoute();syncRouteControls();};
  bind('#calculatePoint',()=>{if(UI.lastPoint)return inspectAt(UI.lastPoint.x,UI.lastPoint.y,true);});
  $('#analysisProfile').onchange=()=>{syncProfileControls();if(UI.lastPoint)inspectAt(UI.lastPoint.x,UI.lastPoint.y,true).catch(e=>toast(e.message));};
  bind('#assumptionInfo',()=>ask('Облачность и непрозрачность','Подтверждение относится только к выбранному пикселю. Оно должно опираться на дополнительный анализ или независимую маску, а не только на цвет. Для высоты принимается Tя9 = T вершины; атмосферная поправка не выполнена.'));
  $('#product').onchange=()=>{clearRange();productHint();requestBuild();};
  $('#channel').onchange=()=>{clearRange();productHint();requestBuild();};
  $('#preset').onchange=async()=>{
    clearMapProduct();await setMap($('#preset').value);requestBuild();
  };
  $('#displayMin').onchange=requestBuild;$('#displayMax').onchange=requestBuild;
  const oldDraw=$('#drawRoute'),newDraw=oldDraw.cloneNode(true);oldDraw.replaceWith(newDraw);
  bind('#drawRoute',()=>{if(!S.product){left('catalog');return;}setDrawing(!S.drawing);});
  const oldUndo=$('#undoRoute'),newUndo=oldUndo.cloneNode(true);oldUndo.replaceWith(newUndo);
  bind('#undoRoute',async()=>{UI.waypointInvalid=false;const rows=$('#routePoints').value.trim().split('\n');rows.pop();$('#routePoints').value=rows.join('\n');await redrawRouteDraft();});
  const oldClear=$('#clearRoute'),newClear=oldClear.cloneNode(true);oldClear.replaceWith(newClear);
  bind('#clearRoute',async()=>{UI.waypointInvalid=false;$('#routePoints').value='';await redrawRouteDraft();});
  bind('#finishDrawing',()=>{setDrawing(false);tab('route');});
  $('#routePoints').oninput=()=>{UI.waypointInvalid=false;invalidateRoute();};
  $('#routePoints').onchange=()=>redrawRouteDraft().catch(e=>toast(e.message));
  for(const id of ['#departure','#speed','#routeStep','#routeProfile','#routeAltitude']){
    $(id).addEventListener('input',invalidateRoute);$(id).addEventListener('change',()=>{invalidateRoute();syncProfileControls();});
  }
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
  $('#calibrationDialog').addEventListener('close',()=>{if(UI.calSaved&&S.scene){UI.calSaved=false;clearRange();requestBuild();}});
  tasks();installTooltips();
  const helpObserver=new MutationObserver(()=>{for(const a of $$('a[href^="/docs/"]')){const href=a.getAttribute('href');if(/\.md(?:#.*)?$/.test(href))a.setAttribute('href','/docs/index.html#'+href.split('/').at(-1).split('.')[0].toLowerCase());}});
  helpObserver.observe(document.body,{subtree:true,childList:true});
};

(async()=>{
  await boot();
  UI.guides=await api('/api/guides');await refreshProfiles();productHint();
  if(S.product)drawLegend(S.product.legend);
})().catch(e=>{toast(e.message);$('#status').textContent='Откройте полный адрес из окна сервера. '+e.message;});
