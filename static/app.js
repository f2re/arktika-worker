'use strict';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const S={registry:null,day:new Date().toISOString().slice(0,10),month:'',scene:null,scenes:[],catalog:[],fileAssets:[],selection:new Set(),map:null,product:null,view:null,drawing:false,route:null,folderMode:'local',folder:null,generation:0,revision:-1,busy:false,resultKey:'',pending:false,pmcArchiveFields:[],pmcArchivePlan:null,pmcArchivePlanRevision:0,pmcArchiveTimer:null,pmcPrepareTimer:null,pmcPrepareNeedsAction:null,pmcPrepareStatus:'idle'};
const months=['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];
const calibrationNames={unknown:'не задана — DN',assumed:'допущение DN = K',declared:'задана пользователем',metadata:'записана в GeoTIFF'};
const categoryNames={channel:'Канал GeoTIFF',rgb:'RGB / COG',image:'Обзор',raw:'Исходник?',science:'Числовой',metadata:'Метаданные',other:'Прочее'};
const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(x,n=1)=>x===null||x===undefined?'—':Number(x).toLocaleString('ru-RU',{maximumFractionDigits:n});
const size=n=>n===null||n===undefined?'размер неизвестен':n<1048576?num(n/1024)+' КиБ':num(n/1048576)+' МиБ';
const qs=obj=>new URLSearchParams(obj).toString();
let toastTimer;
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,6500);}
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-Arktika-Request':'1'},body:body===undefined?undefined:JSON.stringify(body)});let v;try{v=await r.json();}catch{throw Error('Сервер вернул не JSON. Проверьте окно Python.');}if(!r.ok)throw Error(v.error||`HTTP ${r.status}`);return v;}
async function apiForm(path,form){const r=await fetch(path,{method:'POST',headers:{'X-Arktika-Request':'1'},body:form});let v;try{v=await r.json();}catch{throw Error('Сервер вернул не JSON. Проверьте окно Python.');}if(!r.ok)throw Error(v.error||`HTTP ${r.status}`);return v;}
function bind(id,fn){$(id).addEventListener('click',()=>Promise.resolve().then(fn).catch(e=>toast(e.message)));}
function open(id){$(id).showModal();}
function tab(name){$$('.tabs button').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));$$('.tab-panel').forEach(p=>p.hidden=p.id!=='tab-'+name);}
function channelMeta(ch){return S.registry.channels.find(c=>c.id===Number(ch));}
function productHint(){const p=S.registry.products.find(p=>p.id===$('#product').value);$('#productHint').textContent=p?.meaning||'';$('#channelLabel').hidden=p?.id!=='channel';const c=channelMeta($('#channel').value);$('#channelHint').textContent=p?.id==='channel'?c.description:'';}
function setDate(day){if(!/^\d{4}-\d{2}-\d{2}$/.test(day))return toast('Укажите полную дату.');S.day=day;api('/api/settings',{last_date:day}).catch(e=>toast(e.message));$('#date').value=day;$('#year').value=day.slice(0,4);$('#month').value=Number(day.slice(5,7))-1;S.month=day.slice(0,7);S.selection.clear();S.scene=null;loadDay().catch(e=>toast(e.message));loadCalendar().catch(e=>toast(e.message));}
function shiftMonth(delta){const d=new Date(Date.UTC(Number($('#year').value),Number($('#month').value)+delta,1));setDate(d.toISOString().slice(0,10));}
function selectScene(scene){S.scene=scene;$$('#sessions button').forEach(b=>b.classList.toggle('selected',b.dataset.key===scene.platform+'|'+scene.time));const available=new Set(scene.channels.map(c=>c.channel));$$('#channel option').forEach(o=>{o.disabled=!available.has(Number(o.value));});if(!available.has(Number($('#channel').value)))$('#channel').value=available.has(9)?9:(scene.channels[0]?.channel||9);if(!scene.channels.length&&scene.composites?.length)$('#product').value='archive_rgb';else if($('#product').value==='archive_rgb'&&!scene.composites?.length)$('#product').value='channel';$('#departure').value=scene.time.slice(0,16);$('#mapSubtitle').textContent=scene.platform+' · '+scene.time.slice(0,16).replace('T',' ')+' UTC · '+scene.channels.length+' каналов скачано'+(scene.time_assumed?' · UTC принят по правилу импорта':'');productHint();}
function svgEl(name,attrs={}){const n=document.createElementNS('http://www.w3.org/2000/svg',name);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,String(v));return n;}
function polyline(points,klass){return svgEl('polyline',{points:points.map(p=>p.join(',')).join(' '),class:klass});}
async function setMap(preset,width=1000,product){const c=await api('/api/map?'+qs(product?{product}:{preset,width}));S.map=c;$('#pmcOverlay')?.replaceChildren();const g=c.grid;S.view=[0,0,g.width,g.height];$('#map').setAttribute('viewBox',S.view.join(' '));for(const id of ['#ocean','#raster','#nightRaster','#clipRect']){$(id).setAttribute('width',g.width);$(id).setAttribute('height',g.height);}$('#graticule').replaceChildren(...c.graticule.map(l=>polyline(l,'graticule-line')));$('#coast').replaceChildren(...c.coast.map(l=>polyline(l,'coast-line')));$('#labels').replaceChildren(...c.labels.map(l=>{const t=svgEl('text',{x:l.x,y:l.y});t.textContent=l.text;return t;}));$('#projectionCaption').textContent=g.name+' · '+g.crs;return c;}
let PRODUCT_VIEW_REVISION=0;
function clearMapProduct(){PRODUCT_VIEW_REVISION++;S.product=null;PRODUCT_STYLE_PROFILE=null;PRODUCT_STYLE_PRODUCT_ID=null;$('#productStyleOpen').disabled=true;if($('#productStyleDialog').open)$('#productStyleDialog').close();$('#raster').removeAttribute('href');$('#nightRaster').removeAttribute('href');$('#exportProduct').disabled=true;$('#routeLayer').replaceChildren();$('#pointLayer').replaceChildren();$('#motionLayer').replaceChildren();$('#legend').innerHTML='<p class="empty">Область изменена. Постройте продукт в новой проекции.</p>';}
async function showProduct(p){const revision=++PRODUCT_VIEW_REVISION;await setMap(p.grid.preset,p.grid.width,p.id);if(revision!==PRODUCT_VIEW_REVISION)return;S.product=p;$('#productStyleOpen').disabled=!productStyleable(p);if($('#productStyleDialog').open)$('#productStyleDialog').close();$('#preset').value=p.grid.preset;$('#product').value=p.product;$('#channel').value=p.request.channel||9;$('#mapTitle').textContent=p.title;$('#mapSubtitle').textContent=p.platform+' · '+p.time.replace('T',' ').replace('Z',' UTC')+' · '+p.grid.name;$('#raster').setAttribute('href','/artifact/'+p.id+'/map.png');$('#nightRaster').setAttribute('href','/artifact/'+p.id+'/night.png');$('#exportProduct').disabled=false;$('#scienceBanner').textContent=p.calibration_status==='assumed'?'ИССЛЕДОВАТЕЛЬСКОЕ ДОПУЩЕНИЕ DN = K. Калибровка поставщиком не подтверждена. Цвета и пороги не являются готовыми предупреждениями.':p.calibration_status==='unknown'?'Шкала не объявлена: показаны хранимые значения DN. Физические температуры и производные не подменяются предположениями.':'Калибровка объявлена. Экспериментальные спектральные признаки не являются подтверждёнными опасными явлениями.';$('#routeLayer').replaceChildren();$('#pointLayer').replaceChildren();S.route=null;$('#routeResult').innerHTML='';drawLegend(p.legend);productHint();try{await loadProductStyle(p);if(revision!==PRODUCT_VIEW_REVISION||S.product?.id!==p.id)return;$('#productStyleOpen').disabled=!productStyleable(p);}catch(e){if(revision!==PRODUCT_VIEW_REVISION)return;$('#productStyleOpen').disabled=true;toast('Шкала этого продукта пока недоступна: '+e.message);}}
const PRODUCT_STYLE_PALETTES={'blue-amber':[[24,59,99],[40,125,163],[109,183,184],[228,190,111],[141,61,49]],multicolor:[[68,1,84],[59,82,139],[33,145,140],[94,201,98],[253,231,37]],gray:[[20,20,20],[245,245,245]]};
let PRODUCT_STYLE_PROFILE=null,PRODUCT_STYLE_PRODUCT_ID=null;
function productStyleable(p){return !!p&&Array.isArray(p.files)&&p.files.includes('values.tif')&&['absolute_temperature','temperature_difference','continuous','stored_value'].includes(p.legend?.quantity_kind)&&!!p.passport?.algorithm?.version;}
function productStylePalette(id){if(id==='product-default')return PRODUCT_STYLE_PROFILE?.palette||[];return PRODUCT_STYLE_PALETTES[id]||[];}
function productStyleUsesCelsius(p){return p?.legend?.quantity_kind==='absolute_temperature'&&p.legend.units==='K';}
function productStyleToDisplay(p,value){return productStyleUsesCelsius(p)?Number((value-273.15).toFixed(6)):value;}
function productStyleToStored(p,value){return productStyleUsesCelsius(p)?value+273.15:value;}
function renderProductStylePreview(){const palette=productStylePalette($('#productStylePalette').value);$('#productStylePreview').style.background=palette.length?'linear-gradient(90deg,'+palette.map(c=>'rgb('+c.join(',')+')').join(',')+')':'transparent';}
async function loadProductStyle(p){PRODUCT_STYLE_PROFILE=null;PRODUCT_STYLE_PRODUCT_ID=null;$('#productStyleOpen').disabled=!productStyleable(p);if(!productStyleable(p))return;const identity=p.id+'\0'+p.passport.algorithm.version;const result=await api('/api/product/style?id='+encodeURIComponent(p.id));if(S.product?.id!==p.id||S.product?.passport?.algorithm?.version!==p.passport.algorithm.version)return;const profile=result.style;if(profile.product_id!==p.id||profile.algorithm_version!==p.passport.algorithm.version)throw Error('Сервер вернул шкалу для другого продукта или версии.');PRODUCT_STYLE_PROFILE=profile;PRODUCT_STYLE_PRODUCT_ID=identity;}
async function openProductStyle(){const p=S.product;if(!productStyleable(p))return;try{await loadProductStyle(p);if(S.product?.id!==p.id||!PRODUCT_STYLE_PROFILE)return;const profile=PRODUCT_STYLE_PROFILE;$('#productStyleIdentity').textContent=profile.product_id+' · алгоритм '+profile.algorithm_version;const unit=productStyleUsesCelsius(p)?'°C':(p.legend?.units||'единицы легенды');$('#productStyleUnits').textContent=unit;$('.product-style-units').textContent=unit;$('#productStyleMinimum').value=productStyleToDisplay(p,profile.minimum);$('#productStyleMaximum').value=productStyleToDisplay(p,profile.maximum);$('#productStylePalette').value=Object.prototype.hasOwnProperty.call(PRODUCT_STYLE_PALETTES,profile.palette_id)?profile.palette_id:'product-default';renderProductStylePreview();$('#productStyleStatus').textContent='Профиль загружен для этого продукта. Изменения применятся только после сохранения.';open('#productStyleDialog');}catch(e){toast(e.message);}}
async function saveProductStyle(){const p=S.product,base=PRODUCT_STYLE_PROFILE;if(!productStyleable(p)||!base||PRODUCT_STYLE_PRODUCT_ID!==p.id+'\0'+p.passport.algorithm.version)return toast('Загрузите шкалу текущего продукта заново.');const paletteId=$('#productStylePalette').value;const palette=productStylePalette(paletteId);const displayMinimum=Number($('#productStyleMinimum').value),displayMaximum=Number($('#productStyleMaximum').value);if(!Number.isFinite(displayMinimum)||!Number.isFinite(displayMaximum)||displayMinimum>=displayMaximum){$('#productStyleStatus').textContent='Минимум должен быть конечным числом и меньше максимума.';return;}if(!palette.length){$('#productStyleStatus').textContent='Не удалось выбрать палитру.';return;}const minimum=productStyleToStored(p,displayMinimum),maximum=productStyleToStored(p,displayMaximum);const button=$('#productStyleSave');button.disabled=true;$('#productStyleStatus').textContent='Сохраняю профиль и обновляю изображение…';try{const result=await api('/api/product/style',{id:p.id,style:{...base,palette_id:paletteId,palette,minimum,maximum,scale_mode:'linear'}});if(S.product?.id!==p.id||S.product?.passport?.algorithm?.version!==base.algorithm_version)return;PRODUCT_STYLE_PROFILE=result.style;PRODUCT_STYLE_PRODUCT_ID=p.id+'\0'+base.algorithm_version;S.product=result.product;$('#raster').setAttribute('href','/artifact/'+p.id+'/map.png?style='+Date.now());drawLegend(result.product.legend);$('#productStyleStatus').textContent='Сохранено для этого продукта. Научные значения и паспорт не изменяются.';$('#productStyleDialog').close();toast('Цветовая шкала обновлена только для выбранного продукта.');}catch(e){$('#productStyleStatus').textContent='Не удалось сохранить шкалу: '+e.message;}finally{button.disabled=false;}}
function drawLegend(l){let html=`<p class="pill">${escape(calibrationNames[l.status]||l.status)}</p><p class="hint">${escape(l.meaning||'')}</p>`;const asCelsius=l.quantity_kind==='absolute_temperature'&&l.units==='K';const val=v=>(asCelsius&&v!==null&&typeof v==='number')?v-273.15:v;const unit=asCelsius?'°C':l.units;if(l.palette){const grad=l.palette.map(c=>'rgb('+c.join(',')+')').join(',');html+=`<div class="colorbar" style="background:linear-gradient(to right,${grad})"></div><div class="scale-labels"><span>${num(val(l.display_min))} ${escape(unit)}</span><span>${num(val(l.display_max))} ${escape(unit)}</span></div>`;}if(l.stats){html+=`<div class="metric-grid"><div class="metric"><small>Минимум в области</small><b>${num(val(l.stats.min))} <small>${escape(unit)}</small></b></div><div class="metric"><small>Максимум в области</small><b>${num(val(l.stats.max))} <small>${escape(unit)}</small></b></div></div><p class="micro">${escape(l.data_stats_scope)}</p>`;}if(l.components)html+=l.components.map(c=>{const cAsCelsius=c.quantity_kind==='absolute_temperature'&&c.units==='K';const cVal=v=>(cAsCelsius&&v!==null&&typeof v==='number')?v-273.15:v;const cUnit=cAsCelsius?'°C':c.units;const cRange=`${num(cVal(c.min))}…${num(cVal(c.max))} ${cUnit}`;const cStats=`${num(cVal(c.stats?.min))}…${num(cVal(c.stats?.max))} ${cUnit}`;return `<div class="component" style="border-color:${c.component==='R'?'#d75865':c.component==='G'?'#408c70':'#548ecb'}"><b>${c.component}</b> <code>${escape(c.formula)}</code><small>${cRange} · γ ${c.gamma}</small><small>В области: ${cStats}</small></div>`;}).join('');if(l.classes)html+=l.classes.map(c=>`<div class="legend-item"><span class="swatch" style="background:${c.color}"></span><span>${escape(c.name)}</span></div>`).join('');html+=`<p class="micro">Валидных пикселей: ${num(l.valid_pixels,0)} из ${num(l.total_pixels,0)}.<br>${escape(l.nodata)}</p><details><summary>Качество и происхождение</summary><pre>${escape(JSON.stringify({calibration:l.calibration,flags:l.quality_flags,version:l.version,formula:l.formula},null,2))}</pre></details>`;$('#legend').innerHTML=html;}
function zoom(factor,cx,cy){if(!S.view)return;let[x,y,w,h]=S.view;cx??=x+w/2;cy??=y+h/2;let nw=w*factor,nh=h*factor;if(nw<S.map.grid.width/16||nw>S.map.grid.width*2)return;S.view=[cx+(x-cx)*factor,cy+(y-cy)*factor,nw,nh];$('#map').setAttribute('viewBox',S.view.join(' '));}
function pointerPoint(e){const p=$('#map').createSVGPoint();p.x=e.clientX;p.y=e.clientY;return p.matrixTransform($('#map').getScreenCTM().inverse());}
let drag=null;
$('#map').addEventListener('pointerdown',e=>{if(e.button!==0||!S.view)return;drag={x:e.clientX,y:e.clientY,view:[...S.view],moved:false};$('#map').setPointerCapture(e.pointerId);});
$('#map').addEventListener('pointermove',e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.hypot(dx,dy)>4)drag.moved=true;if(drag.moved){const m=$('#map').getScreenCTM();S.view=[drag.view[0]-dx/m.a,drag.view[1]-dy/m.d,drag.view[2],drag.view[3]];$('#map').setAttribute('viewBox',S.view.join(' '));}});
$('#map').addEventListener('pointerup',e=>{if(!drag)return;const click=!drag.moved;drag=null;try{$('#map').releasePointerCapture(e.pointerId);}catch{}if(click)mapClick(e).catch(err=>toast(err.message));});
$('#map').addEventListener('wheel',e=>{e.preventDefault();const p=pointerPoint(e);zoom(e.deltaY<0?.85:1.18,p.x,p.y);},{passive:false});
async function mapClick(e){if(!S.product)return toast('Сначала постройте продукт.');const p=pointerPoint(e);if(p.x<0||p.y<0||p.x>S.map.grid.width||p.y>S.map.grid.height)return;if(S.drawing){const c=await api('/api/coordinates',{x:p.x,y:p.y,product:S.product.id});const text=$('#routePoints').value.trim();$('#routePoints').value=(text?text+'\n':'')+c.lon.toFixed(5)+', '+c.lat.toFixed(5);$('#routeLayer').append(svgEl('circle',{cx:p.x,cy:p.y,r:4,class:'route-point'}));return;}const r=await api('/api/pixel',{product:S.product.id,x:p.x,y:p.y});$('#pointLayer').replaceChildren(svgEl('circle',{cx:p.x,cy:p.y,r:5,fill:'#ffe18b',stroke:'#132c3a','stroke-width':2}));$('#pixel').innerHTML=`<p><b>${num(r.lat,4)}° с. ш. · ${num(r.lon,4)}° долг.</b></p><p class="micro">${escape(r.time)}<br>Высота Солнца: ${num(r.sun_elevation)}°</p><p>Продукт: <b>${r.product_unit==='RGB'?r.display_rgba.slice(0,3).join(' / '):num(r.product_value)} ${escape(r.product_unit)}</b> · флаги ${r.quality_flags}</p><table><thead><tr><th>Канал</th><th>Значение</th><th>Единицы</th></tr></thead><tbody>${r.channels.map(c=>`<tr><td>${c.channel}<small>${channelMeta(c.channel)?.wavelength_um} мкм</small></td><td>${num(c.value)}</td><td>${c.unit}${c.calibration==='assumed'?' *':''}</td></tr>`).join('')}</tbody></table><p class="micro">${escape(r.note)}</p>`;tab('point');}
function routePoints(){const text=$('#routePoints').value.trim();if(text.startsWith('{')){const g=JSON.parse(text);const geometry=g.geometry||g;if(geometry.type!=='LineString')throw Error('GeoJSON должен содержать LineString.');return geometry.coordinates;}return text.split(/\n+/).filter(Boolean).map(l=>{const a=l.trim().split(/[;,\s]+/).map(Number);if(a.length!==2||a.some(x=>!Number.isFinite(x)))throw Error('Каждая строка: долгота, широта.');return a;});}
function routeChart(r){const rows=r.samples;const ch=rows.some(x=>x.channels['9']!==undefined)?'9':Object.keys(r.units)[0];const valid=rows.map(x=>x.channels[ch]).filter(x=>x!==null&&Number.isFinite(x));if(!valid.length)return '<p class="empty">На маршруте нет валидных пикселей.</p>';let lo=Math.min(...valid),hi=Math.max(...valid);if(lo===hi){lo-=1;hi+=1;}let segments=[],seg=[];for(const x of rows){const v=x.channels[ch];if(v===null){if(seg.length)segments.push(seg);seg=[];continue;}seg.push([30+x.distance_km/Math.max(r.length_km,1)*225,125-(v-lo)/(hi-lo)*100]);}if(seg.length)segments.push(seg);return `<svg viewBox="0 0 280 160"><line x1="30" y1="125" x2="260" y2="125" stroke="#cad6dd"/>${segments.map(s=>`<polyline points="${s.map(p=>p.join(',')).join(' ')}" fill="none" stroke="#0a8290" stroke-width="2"/>`).join('')}<text x="2" y="27" font-size="10" fill="#587080">${num(hi)}</text><text x="2" y="125" font-size="10" fill="#587080">${num(lo)}</text><text x="30" y="145" font-size="10" fill="#587080">0 км</text><text x="205" y="145" font-size="10" fill="#587080">${num(r.length_km,0)} км</text><text x="50" y="15" font-size="10" fill="#587080">Канал ${ch} · ${r.units[ch].unit}</text></svg>`;}
async function calculateRoute(){if(!S.product)throw Error('Сначала постройте продукт.');const r=await api('/api/route',{product:S.product.id,points:routePoints(),step_km:Number($('#routeStep').value),speed_kmh:Number($('#speed').value),departure:$('#departure').value?$('#departure').value+':00Z':''});S.route=r;S.drawing=false;$('#drawRoute').classList.remove('draw-active');$('#drawRoute').textContent='Добавлять точки на карте';const lines=[];let chunk=[];for(const p of r.map_points){if(chunk.length&&Math.abs(chunk.at(-1)[0]-p[0])>S.map.grid.width*.55){lines.push(chunk);chunk=[];}chunk.push(p);}if(chunk.length)lines.push(chunk);$('#routeLayer').replaceChildren(...lines.map(l=>polyline(l,'route-line')));$('#routeResult').innerHTML=`<h3>${num(r.length_km,0)} км · ${r.samples.length} точек</h3>${routeChart(r)}<p class="notice">${escape(r.note)}</p><div class="row"><a href="/route-export/${r.id}/csv">CSV</a><a href="/route-export/${r.id}/geojson">GeoJSON</a><a href="/route-export/${r.id}/json">Полный отчёт</a></div><div class="route-table"><table><thead><tr><th>Км</th><th>Время UTC</th><th>Состояние</th></tr></thead><tbody>${r.samples.map(x=>`<tr><td>${num(x.distance_km,0)}</td><td>${x.eta?escape(x.eta.slice(11,16)):'—'}</td><td>${escape(x.status)}</td></tr>`).join('')}</tbody></table></div>`;}
async function refreshQueue(){const r=await api('/api/queue');$('#queueRows').innerHTML=r.jobs.length?r.jobs.map(j=>`<div class="queue-item"><div class="row"><strong class="grow">${escape(j.filename)}</strong><span class="pill">${escape({queued:'В очереди',running:'Загружается',done:'Готово',paused:'Пауза',error:'Ошибка'}[j.state]||j.state)}</span></div><progress value="${j.done}" max="${j.total||1}"></progress><p class="micro">${size(j.done)} / ${size(j.total)} · ${escape(j.platform)} · ${escape(j.time)}</p>${j.error?'<p class="error-text">'+escape(j.error)+'</p>':''}<div class="row"><button class="jobResume" data-id="${j.id}">Продолжить</button><button class="jobRestart" data-id="${j.id}">Заново</button><button class="jobFolder" data-id="${j.id}">Папка</button></div></div>`).join(''):'<p class="empty">Очередь пуста. Откройте сеанс из каталога и выберите файлы.</p>';$$('.jobResume').forEach(b=>b.onclick=()=>api('/api/queue/action',{action:'resume',id:b.dataset.id}).then(refreshQueue).catch(e=>toast(e.message)));$$('.jobRestart').forEach(b=>b.onclick=async()=>{if(await ask('Повторная загрузка','Удалить только незавершённую часть и начать заново?'))api('/api/queue/action',{action:'restart',id:b.dataset.id}).then(refreshQueue).catch(e=>toast(e.message));});$$('.jobFolder').forEach(b=>b.onclick=()=>api('/api/open-folder',{job:b.dataset.id}).catch(e=>toast(e.message)));}
async function folder(mode){S.folderMode=mode;$('#folderTitle').textContent={local:'Открыть папку GeoTIFF',report:'Импорт отчётов каталога',download:'Папка скачивания'}[mode];await browseFolder(S.folder?.path||'');open('#folderDialog');}
async function browseFolder(path){const r=await api('/api/folders',{path});S.folder=r;$('#folderPath').value=r.path;$('#folderRoots').innerHTML=r.roots.map((p,i)=>`<button data-i="${i}">${escape(p.name)}</button>`).join('');$$('#folderRoots button').forEach(b=>b.onclick=()=>browseFolder(r.roots[Number(b.dataset.i)].path).catch(e=>toast(e.message)));$('#folderRows').innerHTML=r.folders.map((p,i)=>`<button data-i="${i}">▸ ${escape(p.name)}</button>`).join('')||'<p class="empty">Вложенных папок нет. Файлы здесь не отображаются.</p>';$$('#folderRows button').forEach(b=>b.onclick=()=>browseFolder(r.folders[Number(b.dataset.i)].path).catch(e=>toast(e.message)));}

const sourceKindNames={satellite:'Спутниковые данные',reanalysis:'Реанализы',local:'Локальные данные'};
const sourceLayerNotes={
  fields_available:'Поля: загрузка, карта и сопоставление. Доступ проверяется запросом данных.',
  operational:'Доступно в текущей карте.',
};
function drawSources(result){
  const groups=['satellite','reanalysis','local'];
  $('#sourceRows').innerHTML=groups.map(kind=>{
    const rows=result.sources.filter(s=>s.kind===kind);if(!rows.length)return '';
    return '<section class="source-group"><h3>'+escape(sourceKindNames[kind])+'</h3>'+rows.map(s=>{
      const ready=s.status==='ready'||s.status==='credentials_present';
      const status=s.status==='ready'?'Готово':ready?'Реквизиты заданы':'Нужен доступ';
      const action=s.id==='local'?'Открыть GeoTIFF':s.id==='gptl'?'Настроить GPTL':s.auth==='cds'?'Настроить CDS':'Настроить Earthdata';
      return `<article class="source-card"><div class="source-card-head"><div><strong>${escape(s.name)}</strong><small>${escape(s.provider)}</small></div><span class="source-state ${ready?'ready':'missing'}">${escape(status)}</span></div><p>${escape(s.description)}</p><p class="micro">${escape(s.coverage)} · ${escape(sourceLayerNotes[s.layer_status]||'')}</p><div class="row wrap">${s.kind==='reanalysis'?`<button class="primary" data-field-source="${escape(s.id)}">Добавить поле</button>`:''}<button class="tonal" data-source="${escape(s.id)}">${escape(action)}</button><a class="text-button" href="${escape(s.docs)}" target="_blank" rel="noopener">Источник</a></div></article>`;
    }).join('')+'</section>';
  }).join('');
  $$('#sourceRows [data-source]').forEach(button=>button.onclick=()=>openSourceAccess(button.dataset.source).catch(e=>toast(e.message)));
}
async function showSources(){
  const result=await api('/api/sources');drawSources(result);
  if(!$('#sourcesDialog').open)open('#sourcesDialog');
}
async function openSourceAccess(id){
  if($('#sourcesDialog').open)$('#sourcesDialog').close();
  if(id==='local'){await folder('local');return;}
  if(id==='gptl'){
    const r=S.serverState;$('#clientId').value=r?.oauth?.client_id||'';$('#redirectUri').value=r?.oauth?.redirect_uri||location.origin+'/oauth/callback';
    open('#authDialog');return;
  }
  if(id==='era5'||id==='carra2'){$('#cdsToken').value='';open('#cdsAccessDialog');return;}
  if(id==='merra2'){$('#earthdataToken').value='';open('#earthdataAccessDialog');return;}
  throw Error('Неизвестный источник данных.');
}
async function saveSourceAccess(provider,text,format='auto'){
  await api('/api/sources/credentials',{provider,text,format});
  const input=provider==='cds'?'#cdsToken':'#earthdataToken';$(input).value='';
  const dialog=provider==='cds'?'#cdsAccessDialog':'#earthdataAccessDialog';if($(dialog).open)$(dialog).close();
  toast(provider==='cds'?'Доступ CDS сохранён в памяти для ERA5 и CARRA2.':'Доступ Earthdata сохранён в памяти для MERRA-2.');
  await showSources();
}
async function sourceCredentialFile(provider,input){
  const file=input.files[0];input.value='';if(!file)return;
  if(file.size>65536)throw Error('Файл доступа больше 64 КиБ.');
  await saveSourceAccess(provider,await file.text(),'auto');
}
async function clearSourceAccess(provider){
  await api('/api/sources/credentials',{provider,clear:true});
  const dialog=provider==='cds'?'#cdsAccessDialog':'#earthdataAccessDialog';if($(dialog).open)$(dialog).close();
  toast('Реквизиты удалены из памяти процесса.');await showSources();
}

function helpTab(name){$$('#helpTabs button').forEach(b=>b.classList.toggle('active',b.dataset.helpTab===name));$$('.help-tab-panel').forEach(p=>p.hidden=(p.id!=='helpTab-'+name));}
async function help(){const r=S.registry;$('#channelReference').innerHTML=r.channels.map(c=>`<article class="reference-card"><h3>${c.id} · ${c.wavelength_um} мкм · ${escape(c.name)}</h3><p>${escape(c.description)}</p></article>`).join('');$('#productReference').innerHTML=r.products.map(p=>`<article class="reference-card"><h3>${escape(p.title)} <span class="pill">${escape({implemented:'реализовано',experimental:'эксперимент',planned:'план',profile_tool:'профильный расчёт'}[p.status])}</span></h3><p>${escape(p.meaning)}</p>${p.channels?'<p>Каналы: '+p.channels.join(', ')+'</p>':''}</article>`).join('');helpTab('quickstart');open('#helpDialog');}
async function poll(){try{const r=await api('/api/state');S.serverState=r;$('#status').textContent=r.status;$('#statusdot').classList.toggle('busy',r.busy);$('#cancel').hidden=!r.busy;$('#process').disabled=r.busy;$('#calibrationState').textContent=calibrationNames[r.calibration?.mode]||'не задана';$('#downloadPath').textContent=r.download_dir;const q=r.queue_counts||{};$('#queueCount').textContent=(q.queued||0)+(q.running||0);$('#tokenStatus').innerHTML=`<p class="micro">Bearer: ${r.token.present?(r.token.remaining==null?'срок неизвестен':r.token.remaining>0?num(r.token.remaining/60,0)+' мин':'истёк'):'не задан'}<br>STS: ${r.token.sts_expires?new Date(r.token.sts_expires*1000).toISOString():'сессии нет'}<br>Refresh token: ${r.oauth?.refresh_present?'в памяти':'не задан'}</p>`;if(r.revision!==S.revision){S.revision=r.revision;await loadDay();await loadCalendar();}if(S.busy&&!r.busy&&r.result){if(r.result.ok&&r.result.result?.id&&r.result.result?.legend){await showProduct(r.result.result);}else if(r.result.ok&&r.result.result?.vectors){const motion=r.result.result;drawMotion(motion);$('#motionResult').innerHTML=`<p>Векторов: ${motion.vectors.length}; кандидатов вращения: ${motion.candidates.length}.</p><p class="notice">${escape(motion.note)}</p>`;}else if(!r.result.ok)toast(r.result.error);S.pending=false;}S.busy=r.busy;if($('#queueDialog').open)await refreshQueue();}catch(e){$('#status').textContent=e.message;}finally{setTimeout(poll,1800);}}
function drawMotion(r){if(!S.map||S.map.grid.preset!==r.grid.preset||S.map.grid.crs!==r.grid.crs)return;const sx=S.map.grid.width/r.grid.width,sy=S.map.grid.height/r.grid.height;const els=r.vectors.map(v=>svgEl('line',{x1:v.x*sx,y1:v.y*sy,x2:(v.x+v.dx)*sx,y2:(v.y+v.dy)*sy,stroke:'#fbe17a','stroke-width':1.5}));for(const c of r.candidates)els.push(svgEl('circle',{cx:c.x*sx,cy:c.y*sy,r:30*sx,fill:'none',stroke:'#e58fd8','stroke-width':2}));$('#motionLayer').replaceChildren(...els);toast('Слежение показано: отрезки смещений и кандидаты вращения, не ПМЦ.');}
const PMC_SOURCE_LABELS={era5:'ERA5',carra2:'CARRA2',merra2:'MERRA-2'};
function pmcRunPanel(){
  if($('#pmcRunPanel'))return;
  const panel=document.createElement('section');panel.id='pmcRunPanel';panel.className='pmc-run-panel';
  panel.innerHTML=`<h3>Один кэшированный ветровой срок</h3><p class="micro">Экспериментальный расчёт относительной завихрённости 850 гПа. Он не загружает данные и не подтверждает ПМЦ. Для CARRA2 используются 2-D геолокация и производные по криволинейной сетке; реальные метаданные и метеорологическая точность отдельно не валидированы.</p><label>Готовое поле<select id="pmcCachedField"><option value="">Загружаю список…</option></select></label><div class="row"><label>Проекция<select id="pmcRunCrs"><option value="arctic">Арктика · EPSG:3413</option><option value="barents">Баренцево море · EPSG:3995</option><option value="kara">Карское море · EPSG:3576</option></select></label><label>Размер сетки<select id="pmcRunWidth"><option>512</option><option selected>768</option><option>1024</option></select></label></div><details><summary>Добавить спутниковую маску для сравнения</summary><p class="micro">Только локальный пакет из двух одноканальных GeoTIFF 0/1 на одной проецированной сетке и JSON-паспорта. Требуются источник, продукт, срок UTC, идентификатор и точные параметры сетки. Пакет до 16 МиБ; сравнение — пространственное, экспериментальное.</p><p class="micro"><a href="/docs/PMC_OBSERVATIONS.md" target="_blank" rel="noopener">Схема sidecar и пример</a></p><label>Разметка labels.tif<input id="pmcObsLabels" type="file" accept=".tif,.tiff,image/tiff"></label><label>Покрытие coverage.tif<input id="pmcObsCoverage" type="file" accept=".tif,.tiff,image/tiff"></label><label>Паспорт metadata.json<input id="pmcObsMetadata" type="file" accept=".json,application/json"></label><button id="pmcObsPreview" type="button" class="tonal">Проверить пакет без сохранения</button><button id="pmcObsImport" type="button" class="tonal" disabled>Сохранить проверенный пакет</button><p id="pmcObsStatus" class="micro" role="status" aria-live="polite">Сравнение не выбрано.</p><label>Маска для этого расчёта<select id="pmcObservation"><option value="">Без спутникового сравнения</option></select></label></details><button id="pmcRunButton" class="tonal full">Рассчитать кандидаты из кэша</button><div id="pmcRunResult" role="status" aria-live="polite"></div>`;
  const plan=$('#pmcArchivePlan');plan.before(panel);
  $('#pmcRunButton').addEventListener('click',()=>runPmcCached().catch(e=>{pmcClearOverlay();pmcRunMessage(e.message,'error-text');}));
  $('#pmcObsPreview').addEventListener('click',()=>previewPmcObservation().catch(e=>{$('#pmcObsStatus').textContent='Пакет не прошёл предварительную проверку: '+e.message;$('#pmcObsStatus').className='error-text';$('#pmcObsImport').disabled=true;}));
  $('#pmcObsImport').addEventListener('click',()=>importPmcObservation().catch(e=>{$('#pmcObsStatus').textContent='Импорт отклонён: '+e.message;$('#pmcObsStatus').className='error-text';}));
  for(const selector of ['#pmcObsLabels','#pmcObsCoverage','#pmcObsMetadata'])$(selector).addEventListener('change',()=>{delete $('#pmcObsImport').dataset.sourceHash;$('#pmcObsImport').disabled=true;$('#pmcObsStatus').textContent='Файлы изменились. Сначала проверьте пакет заново.';$('#pmcObsStatus').className='micro';});
  for(const selector of ['#pmcCachedField','#pmcRunCrs','#pmcRunWidth','#pmcObservation'])$(selector).addEventListener('change',()=>{
    pmcClearOverlay();pmcClearResult();
    pmcRunMessage('Параметры изменены. Запустите расчёт снова, чтобы обновить результат.');
    $('#pmcRunButton').disabled=!$('#pmcCachedField').value;
  });
}
function pmcObservationForm(){
  const files={labels:$('#pmcObsLabels').files[0],coverage:$('#pmcObsCoverage').files[0],metadata:$('#pmcObsMetadata').files[0]};
  if(Object.values(files).some(file=>!file))throw Error('Выберите labels.tif, coverage.tif и metadata.json.');
  const form=new FormData();for(const [name,file] of Object.entries(files))form.append(name,file,({labels:'labels.tif',coverage:'coverage.tif',metadata:'metadata.json'})[name]);
  return form;
}
async function previewPmcObservation(){
  const button=$('#pmcObsPreview'),fileInputs=['#pmcObsLabels','#pmcObsCoverage','#pmcObsMetadata'].map(s=>$(s));button.disabled=true;$('#pmcObsImport').disabled=true;fileInputs.forEach(input=>input.disabled=true);$('#pmcObsStatus').textContent='Проверяю формат, паспорт, сетку и покрытие без сохранения…';$('#pmcObsStatus').className='micro';
  try{
    const form=pmcObservationForm(),result=await apiForm('/api/pmc/observations/preview',form),summary=result.structural_summary||{},observation=result.observation||{};
    button.dataset.sourceHash=result.source_package_sha256||'';$('#pmcObsImport').dataset.sourceHash=result.source_package_sha256||'';$('#pmcObsImport').disabled=!result.source_package_sha256;
    $('#pmcObsStatus').textContent=`Предпросмотр · ${observation.source||'источник?'} / ${observation.product||'продукт?'} v${observation.product_version||'?'} · ${observation.valid_time||'срок?'} UTC · ${summary.grid?.crs||'CRS?'} ${summary.grid?.width||'?'}×${summary.grid?.height||'?'} · покрытие ${num((summary.coverage_fraction||0)*100,1)}% (${num(summary.covered_pixels,0)} ячеек) · положительные метки внутри покрытия ${num(summary.positive_label_pixels_within_coverage,0)} · неизвестное покрытие ${num(summary.unknown_pixels,0)} ячеек. Не сохранено; корректность меток не проверена.`;
    $('#pmcObsStatus').className='notice';
    return result;
  }finally{button.disabled=false;fileInputs.forEach(input=>input.disabled=false);}
}
async function importPmcObservation(){
  const button=$('#pmcObsImport');if(button.disabled||!button.dataset.sourceHash)throw Error('Сначала выполните предварительный просмотр пакета.');
  const fileInputs=['#pmcObsLabels','#pmcObsCoverage','#pmcObsMetadata'].map(s=>$(s)),form=pmcObservationForm();
  button.disabled=true;$('#pmcObsPreview').disabled=true;fileInputs.forEach(input=>input.disabled=true);$('#pmcObsStatus').textContent='Повторно проверяю выбранные файлы перед сохранением…';$('#pmcObsStatus').className='micro';
  try{
    const check=await apiForm('/api/pmc/observations/preview',form);
    if(check.source_package_sha256!==button.dataset.sourceHash){button.dataset.sourceHash=check.source_package_sha256||'';$('#pmcObsStatus').textContent='Содержимое пакета изменилось после предыдущего просмотра. Проверьте сводку и нажмите сохранение ещё раз.';$('#pmcObsStatus').className='notice';return;}
    const result=await apiForm('/api/pmc/observations/import',form);
    if(result.observation?.source_package_sha256!==check.source_package_sha256)throw Error('Хеш сохранённого пакета не совпал с предварительным просмотром.');
    const select=$('#pmcObservation'),option=document.createElement('option');option.value=result.observation_id;option.textContent=`${result.observation.source} · ${result.observation.valid_time} · ${result.observation.product}`;select.append(option);select.value=option.value;select.dispatchEvent(new Event('change'));$('#pmcObsStatus').textContent='Маска сохранена локально со статусом imported_unverified. Сравнение — пиксельное совпадение масок, не метеорологическая проверка.';$('#pmcObsStatus').className='micro';delete button.dataset.sourceHash;
  }finally{button.disabled=!button.dataset.sourceHash;$('#pmcObsPreview').disabled=false;fileInputs.forEach(input=>input.disabled=false);}
}
function pmcClearResult(){const root=$('#pmcRunResult');for(const link of root.querySelectorAll('a[download][href^="blob:"]'))URL.revokeObjectURL(link.href);root.replaceChildren();}
function pmcRunMessage(message,kind='notice'){const root=$('#pmcRunResult');pmcClearResult();if(!message)return;const p=document.createElement('p');p.className=kind;p.textContent=message;root.append(p);}
function pmcClearOverlay(){const layer=$('#pmcOverlay');if(layer)layer.replaceChildren();}
function pmcCachedChoices(fields){
  const select=$('#pmcCachedField'),eligible=(Array.isArray(fields)?fields:[]).filter(f=>f&&['era5','carra2','merra2'].includes(f.source)&&f.variable==='wind'&&Number(f.level)===850&&!f.difference_of);
  select.replaceChildren();if(!eligible.length){const o=document.createElement('option');o.value='';o.textContent='Нет подходящего кэшированного поля';select.append(o);select.disabled=true;$('#pmcRunButton').disabled=true;return;}
  select.disabled=false;
  for(const f of eligible){const o=document.createElement('option');o.value=f.id;o.textContent=`${PMC_SOURCE_LABELS[f.source]} · ветер 850 гПа · ${String(f.time||'срок неизвестен').replace('T',' ').replace('Z',' UTC')}`;select.append(o);}
  $('#pmcRunButton').disabled=false;
}
function pmcGridForRun(map,area,width){
  const preset=area.preset,definition=S.registry?.presets?.[preset],g=map?.grid;
  if(!definition||!g?.bounds||!['crs','bounds'].every(k=>k in g))throw Error('Не удалось определить границы карты для целевой сетки.');
  const bounds=g.bounds.map(Number);if(bounds.length!==4||!bounds.every(Number.isFinite))throw Error('Границы проекции карты некорректны.');
  const [left,bottom,right,top]=bounds,w=Number(width),h=Math.max(128,Math.round(w*(top-bottom)/(right-left)));
  if(!(w>=256&&w<=2048&&h>=128&&h<=2048))throw Error('Размер целевой сетки вне допустимого диапазона.');
  const dx=(right-left)/w,dy=(top-bottom)/h;
  return {crs:g.crs,width:w,height:h,transform:[dx,0,left,0,-dy,top],resolution_x_m:dx,resolution_y_m:dy};
}
function pmcRenderRunResult(r){
  const root=$('#pmcRunResult');pmcClearResult();
  const box=document.createElement('div');box.className='pmc-run-result';
  const title=document.createElement('strong');title.textContent=`Экспериментальные кандидаты · ${Number(r.candidate_count)||0}`;box.append(title);
  const summary=document.createElement('p');summary.textContent=`${PMC_SOURCE_LABELS[r.source]||r.source} · 850 гПа · ${String(r.valid_time||'срок неизвестен').replace('T',' ').replace('Z',' UTC')} · ${r.algorithm_id||'алгоритм'} ${r.algorithm_version||''}`;box.append(summary);
  if(r.satellite_comparison?.status==='comparison_only'&&r.satellite_comparison.metrics){const m=r.satellite_comparison.metrics,c=r.satellite_comparison.coverage;const comparison=document.createElement('p');comparison.className='notice';comparison.textContent=`Пиксельное сравнение с ${r.satellite_comparison.observation?.source||'наблюдением'} · покрыто ${c?.observed_pixels??0} пикселей · IoU ${m.intersection_over_union==null?'—':Number(m.intersection_over_union).toFixed(3)} · POD ${m.probability_of_detection==null?'—':Number(m.probability_of_detection).toFixed(3)} · FAR ${m.false_alarm_ratio==null?'—':Number(m.false_alarm_ratio).toFixed(3)}. Это не метеорологическая проверка ПМЦ.`;box.append(comparison);}
  else if(r.satellite_comparison?.status==='matched_no_coverage'){const comparison=document.createElement('p');comparison.className='notice';comparison.textContent='Спутниковая маска выбрана, но на сетке нет совместно покрытых валидных ячеек; метрики не рассчитаны.';box.append(comparison);}
  const exportLink=document.createElement('a');exportLink.className='text-button';exportLink.textContent='Скачать отчёт JSON';exportLink.download=`pmc-candidates-${String(r.valid_time||'unknown').replace(/[^0-9TZ]/g,'-')}.json`;exportLink.href=URL.createObjectURL(new Blob([JSON.stringify(r,null,2)],{type:'application/json'}));box.append(exportLink);
  if(Array.isArray(r.candidates)&&r.candidates.length){
    const wrap=document.createElement('div');wrap.className='pmc-run-table';wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label','Таблица экспериментальных кандидатов; прокручивается по горизонтали');const table=document.createElement('table');table.innerHTML='<thead><tr><th>Кандидат</th><th>Класс</th><th>Пик завихрённости, с⁻¹</th><th>Пикселей</th><th>Разделение области</th><th>Подтверждение</th></tr></thead>';
    const partitionLabel={partitioned_experimental:'разделён экспериментально',single_peak_expanded:'один изолированный пик · вся область',boundary_censored_unpartitioned:'граница/nodata · без разделения',no_isolated_peak_unpartitioned:'пики не изолированы · без разделения',contour_seed_unavailable_unpartitioned:'контур недоступен · без разделения',contour_level_unavailable_unpartitioned:'уровень контура недоступен · без разделения',component_complexity_limit_unpartitioned:'лимит вычислений · без разделения'};
    const body=document.createElement('tbody');for(const c of r.candidates){const tr=document.createElement('tr');for(const value of [c.candidate_id,c.classification,c['peak_850hpa_relative_vorticity_s-1'],c.pixel_count,partitionLabel[c.partition_status]||c.partition_status,c.confirmation_status]){const td=document.createElement('td');td.textContent=value===null||value===undefined?'—':typeof value==='number'?Number(value).toExponential(3):String(value);tr.append(td);}body.append(tr);}table.append(body);wrap.append(table);box.append(wrap);
  }else{const empty=document.createElement('p');empty.className='empty';empty.textContent='В заданной сетке кандидаты не выделены. Это не означает отсутствие ПМЦ.';box.append(empty);}
  const passport=document.createElement('details');const sum=document.createElement('summary');sum.textContent='Паспорт входа и сетки';const pre=document.createElement('pre');pre.textContent=JSON.stringify({field_id:r.field_id,source:r.source,valid_time:r.valid_time,algorithm_id:r.algorithm_id,algorithm_version:r.algorithm_version,grid:r.grid,field_passport:r.field_passport,input_coverage:r.input_coverage,parameters:r.parameters},null,2);passport.append(sum,pre);box.append(passport);
  const limits=document.createElement('ul');for(const limitation of [...(Array.isArray(r.limitations)?r.limitations:[]),...(r.satellite_comparison?.note?[`Спутниковое сравнение: ${r.satellite_comparison.note}`]:[])]){const li=document.createElement('li');li.textContent=limitation;limits.append(li);}if(limits.childElementCount){const note=document.createElement('div');note.className='notice';note.append(limits);box.append(note);}
  const overlayNote=document.createElement('p');overlayNote.className='micro';const drawn=pmcDrawOverlay(r);overlayNote.textContent=!(r.candidates||[]).length?'Кандидатов нет; точки на карту не нанесены.':drawn?'Фиолетовые точки на карте — положения пиков экспериментальных кандидатов, не места зарождения или подтверждения ПМЦ.':'Наложение не показано: текущая карта использует другую проекцию или область.';box.append(overlayNote);
  root.append(box);
}
function pmcDrawOverlay(r){
  pmcClearOverlay();const g=S.map?.grid,rg=r.grid;if(!g||!rg||g.crs!==rg.crs||!Array.isArray(g.bounds)||!Array.isArray(rg.transform))return false;
  const transform=rg.transform;if(transform.length!==6||!transform.every(Number.isFinite))return false;
  const left=transform[2],top=transform[5],right=left+transform[0]*rg.width,bottom=top+transform[4]*rg.height;
  const targetBounds=[left,bottom,right,top];if(g.bounds.length!==4||targetBounds.some((v,i)=>Math.abs(Number(g.bounds[i])-v)>Math.max(1,Math.abs(v))*1e-8))return false;
  let layer=$('#pmcOverlay');if(!layer){layer=svgEl('g',{id:'pmcOverlay','aria-label':'Экспериментальные точки кандидатов'});$('#mapContent').append(layer);}
  for(const candidate of r.candidates||[]){const rc=candidate.peak_pixel_rc;if(!Array.isArray(rc)||rc.length!==2||!rc.every(Number.isFinite))continue;const x=(Number(rc[1])+.5)*g.width/rg.width,y=(Number(rc[0])+.5)*g.height/rg.height;
    layer.append(svgEl('circle',{cx:x,cy:y,r:8,fill:'#e879f9','fill-opacity':'.22',stroke:'#7e22ce','stroke-width':2,'vector-effect':'non-scaling-stroke','data-candidate':candidate.candidate_id||''}));
    layer.append(svgEl('circle',{cx:x,cy:y,r:2.4,fill:'#7e22ce','vector-effect':'non-scaling-stroke'}));
  }
  return layer.childElementCount>0;
}
async function runPmcCached(){
  const fieldId=$('#pmcCachedField').value;if(!fieldId)throw Error('В кэше нет подходящего ветрового поля ERA5/CARRA2/MERRA-2 850 гПа. Откройте «Пространственные поля» и подготовьте срок отдельно.');
  const controls=['#pmcCachedField','#pmcRunCrs','#pmcRunWidth','#pmcObservation'].map(selector=>$(selector));
  const selection=controls.map(control=>control.value);
  pmcClearOverlay();$('#pmcRunButton').disabled=true;controls.forEach(control=>control.disabled=true);pmcRunMessage('Считаю только выбранное поле из локального кэша…');
  try{
    const area=await api('/api/map?'+qs({preset:selection[1],width:selection[2]}));
    const target_grid=pmcGridForRun(area,{preset:selection[1]},selection[2]);
    const observationId=controls[3]?.value;
    const body={field_id:fieldId,target_grid};if(observationId)body.observation_id=observationId;
    const result=await api('/api/pmc/run',body);
    if(result.status!=='completed'||result.maturity!=='experimental'||result.field_id!==fieldId)throw Error('Ответ API не соответствует контракту экспериментального расчёта.');
    if(controls.some((control,index)=>control.value!==selection[index]))throw Error('Параметры изменились во время расчёта. Запустите его снова.');
    pmcRenderRunResult(result);
  }finally{controls.forEach(control=>control.disabled=false);$('#pmcRunButton').disabled=!$('#pmcCachedField').value;}
}
function pmcFeedback(message,kind='notice'){$('#pmcArchiveFeedback').replaceChildren();if(!message)return;const p=document.createElement('p');p.className=kind;p.textContent=message;$('#pmcArchiveFeedback').append(p);}
function pmcRenderSources(sources){
  const root=$('#pmcArchiveSources');root.replaceChildren();
  const rows=Array.isArray(sources)?sources.filter(s=>s&&PMC_SOURCE_LABELS[s.id]):[];
  if(!rows.length){root.textContent='Каталог адаптеров недоступен; отправка плана отключена до ответа сервера.';$('#pmcArchivePlan').disabled=true;return;}
  for(const source of rows){const label=document.createElement('label');label.className='check';const input=document.createElement('input');input.type='checkbox';input.name='pmcArchiveSource';input.value=source.id;input.checked=true;const text=document.createElement('span');text.textContent=`${PMC_SOURCE_LABELS[source.id]} — адаптер зарегистрирован; доступ к аккаунту и наличие поля 850 гПа проверяются отдельно`;label.append(input,text);root.append(label);}
  $('#pmcArchivePlan').disabled=false;
}
function pmcObservationChoices(observations){
  const select=$('#pmcObservation'),items=Array.isArray(observations)?observations:[];select.replaceChildren();
  const empty=document.createElement('option');empty.value='';empty.textContent='Без спутникового сравнения';select.append(empty);
  for(const item of items){if(!item||typeof item.observation_id!=='string')continue;const option=document.createElement('option');option.value=item.observation_id;option.textContent=`${item.source} · ${item.valid_time} · ${item.product}`;select.append(option);}
  $('#pmcObsStatus').textContent=items.length?`Доступны ранее импортированные маски: ${items.length}.`:'Сравнение не выбрано.';
}
async function openPmcArchive(){
  pmcInvalidateArchivePlan();pmcRunPanel();pmcClearOverlay();pmcClearResult();pmcCachedChoices([]);$('#pmcCachedField').options[0].textContent='Загружаю список…';$('#pmcArchivePlan').disabled=true;$('#pmcArchiveSources').textContent='Проверяю зарегистрированные адаптеры…';open('#pmcArchiveDialog');
  try{const state=await api('/api/reanalysis/state');S.pmcArchiveFields=state.fields||[];pmcRenderSources(state.catalogue?.sources||[]);pmcCachedChoices(state.fields||[]);const observations=await api('/api/pmc/observations');pmcObservationChoices(observations.observations||[]);await pmcRefreshArchiveTask();}
  catch(e){$('#pmcArchiveSources').textContent='Не удалось прочитать каталог адаптеров.';pmcFeedback('Проверка архива недоступна: '+e.message,'error-text');}
}
function pmcArchiveBody(){
  const sources=$$('[name="pmcArchiveSource"]:checked').map(e=>e.value);
  const start=$('#pmcArchiveStart').value.trim(),end=$('#pmcArchiveEnd').value.trim();
  const utc=/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
  if(!sources.length)throw Error('Выберите хотя бы один источник.');
  if(!utc.test(start)||!utc.test(end)||!Number.isFinite(Date.parse(start))||!Number.isFinite(Date.parse(end)))throw Error('Укажите начало и конец в UTC формате YYYY-MM-DDTHH:MM:SSZ.');
  if(Date.parse(start)>=Date.parse(end))throw Error('Начало интервала должно быть раньше конца.');
  const area=['#pmcNorth','#pmcWest','#pmcSouth','#pmcEast'].map(id=>Number($(id).value));
  if(area.some((v,i)=>!$(['#pmcNorth','#pmcWest','#pmcSouth','#pmcEast'][i]).value.trim()||!Number.isFinite(v)))throw Error('Задайте все четыре границы региона.');
  const [north,west,south,east]=area;
  if(north>90||north< -90||south>90||south< -90||west>180||west< -180||east>180||east< -180||north<=south)throw Error('Проверьте границы: широты от −90 до 90, долготы от −180 до 180, север выше юга.');
  if(west>=east)throw Error('Область через антимеридиан пока не поддерживается: западная граница должна быть меньше восточной.');
  return {sources,start,end,area};
}
function pmcInvalidateArchivePlan(message,kind='notice'){
  S.pmcArchivePlanRevision++;
  S.pmcArchivePlan=null;
  const runner=$('#pmcArchiveRunner');if(runner)runner.hidden=true;
  $('#pmcArchiveFields').replaceChildren();$('#pmcArchiveRun').disabled=true;
  const planButton=$('#pmcArchivePlan');if(planButton)planButton.disabled=!$('#pmcArchiveSources').querySelector('[name="pmcArchiveSource"]');
  pmcFeedback(message||'',kind);
  return S.pmcArchivePlanRevision;
}
async function planPmcArchive(){
  const revision=pmcInvalidateArchivePlan();
  let body;try{body=pmcArchiveBody();pmcFeedback('Запрашиваю только план и проверку входных данных; загрузка не запускается…');$('#pmcArchivePlan').disabled=true;
    const result=await api('/api/pmc/archive/plan',body);
    if(revision!==S.pmcArchivePlanRevision)return;
    const plan=result?.plan;
    if(!plan||!Array.isArray(plan.area)||plan.area.length!==4||plan.area.some((value,index)=>!Number.isFinite(Number(value))||Number(value)!==body.area[index])||JSON.stringify(plan.sources)!==JSON.stringify(body.sources)||plan.start!==body.start||plan.end!==body.end)throw Error('Серверный план не совпадает с выбранными источниками, сроками и регионом. Проверьте план ещё раз.');
    S.pmcArchiveFields=[];try{const cache=await api('/api/reanalysis/state');if(revision!==S.pmcArchivePlanRevision)return;S.pmcArchiveFields=cache.fields||[];}catch{}
    $('#pmcArchiveFeedback').replaceChildren();S.pmcArchivePlan=plan;
    const box=document.createElement('div');box.className='notice';const title=document.createElement('strong');title.textContent='Экспериментальный план · кандидатный анализ';box.append(title);
    const lines=[];
    if(result.maturity==='experimental')lines.push('Зрелость: экспериментальная.');
    if(result.output_status==='candidate_only')lines.push('Статус продукции: только кандидаты.');
    if(result.forecast===false)lines.push('Прогноз не рассчитывается.');
    if(typeof result.message==='string')lines.push(result.message);
    if(result.plan&&Number.isInteger(result.plan.step_count))lines.push(`Шагов плана: ${result.plan.step_count}; загрузка не запускалась.`);
    if(result.plan&&typeof result.plan.start==='string'&&typeof result.plan.end==='string')lines.push(`Интервал: ${result.plan.start} — ${result.plan.end}.`);
    if(Array.isArray(result.sources))for(const item of result.sources){
      const name=PMC_SOURCE_LABELS[item.id]||item.name||item.id;if(!name)continue;
      const credentials=item.credential_status==='ready'?'реквизиты доступны':item.credential_status==='required'?'нужны реквизиты':'статус реквизитов не проверен';
      const support=item.support_status==='wind_850_available'?'вход ветра на 850 гПа заявлен':item.support_status==='unsupported'?'источник не поддерживает нужное поле':'поддержка поля не подтверждена';
      lines.push(`${name}: ${credentials}; ${support}${Number.isFinite(item.interval_hours)?`; шаг ${item.interval_hours} ч`:''}${Number.isInteger(item.first_year)?`; с ${item.first_year} г.`:''}`);
    }
    if(Array.isArray(result.missing_inputs)&&result.missing_inputs.length)lines.push('Недостающие входы: '+result.missing_inputs.join(', '));
    if(result.satellite&&typeof result.satellite.note==='string')lines.push('Спутник: '+result.satellite.note);
    if(Array.isArray(result.limitations))for(const item of result.limitations)if(typeof item==='string')lines.push('Ограничение: '+item);
    if(!lines.length)lines.push('Ответ сервера не содержит полного плана. Расчёт и загрузка не запускаются.');
    const list=document.createElement('ul');for(const line of lines){const li=document.createElement('li');li.textContent=line;list.append(li);}box.append(list);
    const note=document.createElement('p');note.textContent='Это план архивной обработки, не прогноз. Все обнаружения остаются кандидатами до независимого сравнения со спутниковой разметкой.';box.append(note);$('#pmcArchiveFeedback').append(box);
    pmcRenderArchiveFields(result.plan);
  }catch(e){if(revision===S.pmcArchivePlanRevision){const message=e.message||'Не удалось составить план.';$('#pmcArchiveRunner').hidden=true;$('#pmcArchiveFields').replaceChildren();$('#pmcArchiveRun').disabled=true;pmcFeedback(message.includes('404')||message.includes('Not Found')?'Серверный API планирования архива ещё недоступен. Никакие данные не загружены.':message,'error-text');}}
  finally{if(revision===S.pmcArchivePlanRevision)$('#pmcArchivePlan').disabled=false;}
}
function pmcRenderArchiveFields(plan){
  const runner=$('#pmcArchiveRunner'),root=$('#pmcArchiveFields');if(!plan||!Array.isArray(plan.sources)){runner.hidden=true;return;}
  runner.hidden=false;root.replaceChildren();const expected=Object.fromEntries(plan.sources.map(source=>[source,new Set()]));
  const intervals={era5:1,carra2:3,merra2:3};for(const source of plan.sources){const step=intervals[source]||1;let time=Date.parse(plan.start);while(time<=Date.parse(plan.end)&&new Date(time).getUTCHours()%step!==0)time+=3600000;for(;time<=Date.parse(plan.end);time+=step*3600000)expected[source].add(new Date(time).toISOString().replace('.000Z','Z'));}
  const fields=S.pmcArchiveFields.filter(field=>expected[field.source]?.has(String(field.time||'').replace('+00:00','Z'))&&field.variable==='wind'&&Number(field.level)===850&&field.time_kind==='instantaneous_analysis'&&!field.difference_of);
  if(!fields.length){const p=document.createElement('p');p.className='empty';p.textContent='Подходящих сохранённых полей пока нет. Подготовка загрузит недостающие сроки; детектор останется отдельным действием.';root.append(p);$('#pmcArchiveRun').disabled=true;$('#pmcArchivePrepare').disabled=['running','needs_action','ready'].includes(S.pmcPrepareStatus);return;}
  for(const field of fields){const label=document.createElement('label');label.className='check';const input=document.createElement('input');input.type='checkbox';input.name='pmcArchiveField';input.value=field.id;input.checked=true;input.disabled=S.pmcPrepareStatus==='running';const text=document.createElement('span');text.textContent=`${PMC_SOURCE_LABELS[field.source]||field.source} · ${String(field.time).replace('T',' ').replace('Z',' UTC')}`;label.append(input,text);root.append(label);}
  $('#pmcArchiveRun').disabled=S.pmcPrepareStatus==='running';$('#pmcArchivePrepare').disabled=['running','needs_action','ready'].includes(S.pmcPrepareStatus);
}
async function pmcPrepareArchive(){
  const plan=S.pmcArchivePlan;if(!plan)throw Error('Сначала проверьте план архива.');
  const revision=S.pmcArchivePlanRevision;
  const current=pmcArchiveBody();if(JSON.stringify(current)!==JSON.stringify({sources:plan.sources,start:plan.start,end:plan.end,area:plan.area}))throw Error('Параметры плана изменились. Составьте план заново.');
  const {target_grid}=await api('/api/pmc/archive/grid',{preset:$('#preset').value,width:$('#pmcRunWidth').value,area:plan.area});
  if(revision!==S.pmcArchivePlanRevision||S.pmcArchivePlan!==plan)throw Error('Параметры изменились при подготовке сетки. Загрузка не запускалась; составьте план заново.');
  await api('/api/pmc/archive/prepare',{plan,target_grid});await pmcRefreshArchivePrepare();
}
async function pmcRefreshArchivePrepare(){
  const root=$('#pmcArchivePrepareTask');if(!root)return;
  try{const task=await api('/api/pmc/archive/prepare/state');
    S.pmcPrepareStatus=task.status||'idle';
    if(task.status==='idle'){root.replaceChildren();root.textContent='Подготовка входных полей ещё не запускалась.';$('#pmcArchivePrepareResume').hidden=true;return;}
    const progress=`${task.message||task.status} · готово ${task.prepared||0} из ${task.total||0}`;
    let progressNode=root.querySelector('[data-pmc-progress]');
    if(!progressNode){progressNode=document.createElement('p');progressNode.dataset.pmcProgress='';root.prepend(progressNode);}
    if(root.dataset.progress!==progress){progressNode.textContent=progress;root.dataset.progress=progress;}
    root.querySelectorAll('[data-pmc-action]').forEach(node=>node.remove());
    if(task.status==='needs_action'&&task.needs_action){S.pmcPrepareNeedsAction=task.needs_action;const button=document.createElement('button');button.type='button';button.dataset.pmcAction='';button.className='tonal';button.textContent='Настроить доступ к '+(PMC_SOURCE_LABELS[task.needs_action.source]||task.needs_action.source);button.onclick=()=>{FIELDS.accessReturn=task.needs_action.source;openSourceAccess(task.needs_action.source);};root.append(button);}
    else if(task.status==='ready'&&root.dataset.readyId!==task.id){
      S.pmcPrepareNeedsAction=null;
      try{const state=await api('/api/reanalysis/state');S.pmcArchiveFields=state.fields||[];}catch{}
      pmcRenderArchiveFields(S.pmcArchivePlan);
      root.dataset.readyId=task.id;
    }
    $('#pmcArchivePrepare').disabled=['running','needs_action','ready'].includes(task.status);
    $('#pmcArchivePrepareResume').hidden=!(task.resume_available&&['needs_action','interrupted','cancelled','error'].includes(task.status));
    $('#pmcArchiveCancel').hidden=!['running','needs_action'].includes(task.status);
    const preparing=task.status==='running';$('#pmcArchiveRun').disabled=preparing||!$('#pmcArchiveFields').querySelector('[name="pmcArchiveField"]');
    $$('#pmcArchiveFields [name="pmcArchiveField"]').forEach(input=>{input.disabled=preparing;});
    if(task.status==='running'){if(S.pmcPrepareTimer)clearTimeout(S.pmcPrepareTimer);S.pmcPrepareTimer=setTimeout(()=>pmcRefreshArchivePrepare().catch(()=>{}),1300);}
  }catch(e){root.textContent='Состояние подготовки недоступно: '+e.message;}
}
async function pmcStartArchive(){
  const plan=S.pmcArchivePlan,field_ids=$$('[name="pmcArchiveField"]:checked').map(e=>e.value);if(!plan||!field_ids.length)throw Error('Сначала проверьте план и выберите кэшированные сроки.');
  const revision=S.pmcArchivePlanRevision;
  const current=pmcArchiveBody();if(JSON.stringify(current)!==JSON.stringify({sources:plan.sources,start:plan.start,end:plan.end,area:plan.area}))throw Error('Параметры плана изменились. Составьте план заново.');
  if(field_ids.some(id=>!S.pmcArchiveFields.some(f=>f.id===id&&['era5','carra2','merra2'].includes(f.source))))throw Error('Выберите кэшированные поля ветра 850 гПа из поддерживаемых адаптеров ERA5, CARRA2 или MERRA-2.');
  const preset=$('#pmcRunCrs').value||'arctic',width=Number($('#pmcRunWidth').value||'768');
  const {target_grid}=await api('/api/pmc/archive/grid',{preset,width,area:plan.area});
  if(revision!==S.pmcArchivePlanRevision||S.pmcArchivePlan!==plan)throw Error('Параметры изменились при подготовке сетки. Составьте план заново.');
  await api('/api/pmc/archive/run',{plan,field_ids,target_grid});await pmcRefreshArchiveTask();
}
function pmcRenderArchiveEvidence(report,root){
  const rows=[];for(const group of Array.isArray(report?.tracks_by_source)?report.tracks_by_source:[]){
    for(const evidence of Array.isArray(group?.candidate_emergence_evidence)?group.candidate_emergence_evidence:[])rows.push(evidence);
  }
  const panel=document.createElement('details');panel.className='pmc-onset-evidence';
  const summary=document.createElement('summary');summary.textContent=`Первые появления модельных кандидатов · ${rows.length}`;panel.append(summary);
  const disclaimer=document.createElement('p');disclaimer.className='micro';disclaimer.textContent='Первая фиксация модельного кандидата не является физическим зарождением ПМЦ. Совпадения ниже показывают только точные сравнения кандидатов и спутниковых масок.';panel.append(disclaimer);
  if(rows.length){
    const wrap=document.createElement('div');wrap.className='pmc-run-table';wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label','Свидетельства первых появлений кандидатов');
    const table=document.createElement('table');table.innerHTML='<thead><tr><th>Источник</th><th>Первый срок UTC</th><th>Кандидат</th><th>Предыдущий кадр</th><th>Сравнение источников</th><th>Спутниковая маска</th></tr></thead>';
    const body=document.createElement('tbody');for(const e of rows){
      const pairs=e.cross_source_evidence_at_first_detection?.pairs||[];
      const cross=pairs.length?pairs.map(p=>`${p.other_source||'?'}: ${p.status==='matched_candidate'?'совпадение '+(p.matches||[]).map(m=>m.candidate_id||'?').join(', '):p.status||'неизвестно'}`).join('; '):(e.cross_source_evidence_at_first_detection?.status||'не сравнивалось');
      const satellite=e.satellite_evidence_at_first_detection||{},observation=satellite.observation||{},coverage=satellite.coverage||{};
      const sat=satellite.status==='comparison_only'?`${observation.source||'?'} / ${observation.product_id||'?'} · ${observation.observation_id||'?'} · покрыто ${coverage.observed_pixels??'?'} px`:(satellite.reason_code||satellite.status||'неизвестно');
      const tr=document.createElement('tr');for(const value of [e.source,e.first_detected_time,e.candidate_id,e.prior_frame?.status,cross,sat]){const td=document.createElement('td');td.textContent=value===undefined||value===null?'—':String(value);tr.append(td);}body.append(tr);
    }
    table.append(body);wrap.append(table);panel.append(wrap);
  }else{const empty=document.createElement('p');empty.className='empty';empty.textContent='Для этого отчёта нет записей о первых появлениях.';panel.append(empty);}
  root.append(panel);
}
async function pmcShowArchiveEvidence(id,root){
  const button=document.createElement('button');button.type='button';button.className='tonal';button.textContent='Показать первые появления и сравнения';
  button.onclick=async()=>{button.disabled=true;try{const report=await api('/api/pmc/archive/report?id='+encodeURIComponent(id));pmcRenderArchiveEvidence(report,root);button.remove();}catch(e){button.disabled=false;pmcFeedback('Отчёт о первых появлениях недоступен: '+e.message,'error-text');}};
  root.append(button);
}
async function pmcRefreshArchiveTask(){
  const root=$('#pmcArchiveTask');if(!root)return;try{const task=await api('/api/pmc/archive/state');root.replaceChildren();
    if(task.status==='idle'){root.textContent='Архивный анализ ещё не запускался.';$('#pmcArchiveResume').hidden=true;await pmcRefreshArchivePrepare();return;}
    const p=document.createElement('p');p.textContent=`${task.message||task.status} · обработано ${task.completed||0} из ${task.total||0}${task.failed?` · ошибок ${task.failed}`:''}`;root.append(p);
    if(task.id){const report=document.createElement('a');report.className='text-button';report.href='/api/pmc/archive/report?id='+encodeURIComponent(task.id);report.download=`pmc-archive-${task.id}.json`;report.textContent='Скачать текущий отчёт JSON';root.append(report);if(task.status!=='running')pmcShowArchiveEvidence(task.id,root);}
    $('#pmcArchiveCancel').hidden=task.status!=='running';$('#pmcArchiveResume').hidden=!(task.resume_available&&['interrupted','cancelled','error','partial'].includes(task.status));
    await pmcRefreshArchivePrepare();
    if(task.status==='running'){if(S.pmcArchiveTimer)clearTimeout(S.pmcArchiveTimer);S.pmcArchiveTimer=setTimeout(()=>pmcRefreshArchiveTask().catch(()=>{}),1300);}
  }catch(e){root.textContent='Состояние архивной задачи недоступно: '+e.message;}
}
function registerEvents(){
 $$('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());$$('.tabs button[data-tab]').forEach(b=>b.onclick=()=>tab(b.dataset.tab));$$('#helpTabs button').forEach(b=>b.onclick=()=>helpTab(b.dataset.helpTab));
 bind('#monthPrev',()=>shiftMonth(-1));bind('#monthNext',()=>shiftMonth(1));$('#month').onchange=()=>setDate($('#year').value+'-'+String(Number($('#month').value)+1).padStart(2,'0')+'-01');$('#year').onchange=$('#month').onchange;$('#date').onchange=()=>setDate($('#date').value);bind('#today',()=>setDate(new Date().toISOString().slice(0,10)));$('#platform').onchange=()=>setDate(S.day);
 bind('#searchDay',async()=>{await api('/api/search',{date:S.day,platform:$('#platform').value});S.busy=true;});bind('#searchMonth',async()=>{if(await ask('Каталог за месяц','Получить все сроки выбранного месяца?')){await api('/api/search',{date:S.day,scope:'month',platform:$('#platform').value});S.busy=true;}});bind('#cancel',()=>api('/api/cancel',{}));
 bind('#localImport',()=>folder('local'));bind('#catalogImport',()=>folder('report'));bind('#downloadFolder',()=>folder('download'));bind('#folderGo',()=>browseFolder($('#folderPath').value));$('#folderPath').onkeydown=e=>{if(e.key==='Enter')browseFolder($('#folderPath').value).catch(e=>toast(e.message));};bind('#folderUp',()=>browseFolder(S.folder.parent));bind('#createFolder',async()=>{const name=prompt('Имя новой папки');if(name){await api('/api/folders/create',{parent:S.folder.path,name});await browseFolder(S.folder.path);}});bind('#chooseFolder',async()=>{const p=S.folder.path;if(S.folderMode==='download')await api('/api/settings',{download_dir:p});else{await api(S.folderMode==='local'?'/api/local/import':'/api/import',{path:p});S.busy=true;}$('#folderDialog').close();toast('Папка выбрана: '+p);});
 bind('#previousScene',()=>{const i=S.scenes.findIndex(s=>s.id===S.scene?.id);if(i>0)selectScene(S.scenes[i-1]);});bind('#nextScene',()=>{const i=S.scenes.findIndex(s=>s.id===S.scene?.id);if(i<S.scenes.length-1)selectScene(S.scenes[i+1]);});
 $('#product').onchange=productHint;$('#channel').onchange=productHint;$('#preset').onchange=async()=>{try{clearMapProduct();await setMap($('#preset').value);}catch(e){toast(e.message);}};bind('#process',requestBuild);
 bind('#zoomIn',()=>zoom(.8));bind('#zoomOut',()=>zoom(1.25));bind('#mapReset',()=>{S.view=[0,0,S.map.grid.width,S.map.grid.height];$('#map').setAttribute('viewBox',S.view.join(' '));});$('#nightToggle').onchange=()=>$('#nightRaster').setAttribute('visibility',$('#nightToggle').checked?'visible':'hidden');$('#layerToggle').onchange=()=>$('#raster').setAttribute('visibility',$('#layerToggle').checked?'visible':'hidden');bind('#exportProduct',()=>{if(S.product)location.href='/artifact/'+S.product.id+'/export.zip';});bind('#productStyleOpen',openProductStyle);bind('#productStyleSave',saveProductStyle);$('#productStylePalette').onchange=renderProductStylePreview;
 bind('#helpOpen',help);bind('#authOpen',showSources);bind('#cdsChooseFile',()=>$('#cdsCredentialFile').click());$('#cdsCredentialFile').onchange=()=>sourceCredentialFile('cds',$('#cdsCredentialFile')).catch(e=>toast(e.message));bind('#cdsSave',()=>saveSourceAccess('cds',$('#cdsToken').value,'token'));bind('#cdsClear',()=>clearSourceAccess('cds'));bind('#earthdataChooseFile',()=>$('#earthdataCredentialFile').click());$('#earthdataCredentialFile').onchange=()=>sourceCredentialFile('earthdata',$('#earthdataCredentialFile')).catch(e=>toast(e.message));bind('#earthdataSave',()=>saveSourceAccess('earthdata',$('#earthdataToken').value,'token'));bind('#earthdataClear',()=>clearSourceAccess('earthdata'));bind('#saveToken',async()=>{await api('/api/credentials',{token:$('#token').value,refresh_token:$('#refreshToken').value});$('#token').value='';$('#refreshToken').value='';toast('Токен принят в память. Проверка доступа выполняется при запросе каталога или объекта.');});bind('#clearToken',()=>api('/api/credentials',{token:''}));bind('#oauthConfigure',()=>api('/api/oauth/configure',{client_id:$('#clientId').value,redirect_uri:$('#redirectUri').value,scope:'openid s3policy offline_access'}));bind('#oauthStart',async()=>{const r=await api('/api/oauth/start',{});location.href=r.url;});bind('#oauthRefresh',async()=>{await api('/api/oauth/refresh',{});toast('Токен обновлён.');});
 bind('#calibrationOpen',()=>{$('#calMode').value=S.serverState?.calibration?.mode||'unknown';$('#calPlatform').value=S.serverState?.calibration?.platform||$('#platform').value||'';$('#calJson').value=JSON.stringify(S.serverState?.calibration?.mode==='declared'?S.serverState.calibration:{reference:'',channels:{}},null,2);open('#calibrationDialog');});bind('#saveCalibration',async()=>{const mode=$('#calMode').value;if((mode==='assumed'||mode==='declared')&&!$('#calPlatform').value)return toast('Выберите аппарат, к которому относится калибровка.');if(mode==='assumed'&&!await ask('Шкала DN = K','Принять хранимые значения за кельвины? Допущение сохранится в результатах.'))return;const cfg=mode==='declared'?JSON.parse($('#calJson').value||'{}'):{};cfg.mode=mode;if(mode==='assumed'||mode==='declared')cfg.platform=$('#calPlatform').value;await api('/api/calibration',cfg);S.serverState.calibration=cfg;UI.calSaved=true;$('#calibrationDialog').close();toast('Калибровка задана для новых расчётов только на выбранном аппарате. Старый продукт не изменён.');});
 initCatalog();
 bind('#queueOpen',async()=>{await refreshQueue();open('#queueDialog');});bind('#queuePause',()=>api('/api/queue/action',{action:'pause'}));bind('#queueResume',()=>api('/api/queue/action',{action:'resume'}));
 bind('#drawRoute',()=>{if(!S.product)throw Error('Сначала постройте продукт.');S.drawing=!S.drawing;$('#drawRoute').classList.toggle('draw-active',S.drawing);$('#drawRoute').textContent=S.drawing?'Режим точек включён — нажмите для выхода':'Добавлять точки на карте';});bind('#undoRoute',()=>{const rows=$('#routePoints').value.trim().split('\n');rows.pop();$('#routePoints').value=rows.join('\n');$('#routeLayer').lastElementChild?.remove();});bind('#clearRoute',()=>{$('#routePoints').value='';$('#routeLayer').replaceChildren();$('#routeResult').innerHTML='';S.route=null;});bind('#calculateRoute',calculateRoute);
 bind('#productsOpen',async()=>{const r=await api('/api/products');$('#savedProducts').innerHTML=r.products.map(p=>`<article class="reference-card"><h3>${escape(p.title)}</h3><p>${escape(p.platform+' · '+p.time+' · '+p.grid.name)} · ${escape(calibrationNames[p.calibration_status])}</p><button data-id="${p.id}">Открыть</button> <a href="/artifact/${p.id}/export.zip">Экспорт с легендой</a></article>`).join('')||'<p class="empty">Продуктов пока нет.</p>';$$('#savedProducts button').forEach(b=>b.onclick=()=>{showProduct(r.products.find(p=>p.id===b.dataset.id)).then(()=>$('#productsDialog').close()).catch(e=>toast(e.message));});open('#productsDialog');});
 bind('#labOpen',()=>open('#labDialog'));bind('#profileCalculate',async()=>{$('#profileResult').textContent=JSON.stringify(await api('/api/profile',JSON.parse($('#profileJson').value)),null,2);});bind('#motionOpen',async()=>{const r=await api('/api/scenes');const opts=r.scenes.map(s=>`<option value="${s.id}">${escape(s.platform+' · '+s.time)}</option>`).join('');$('#motionFirst').innerHTML=opts;$('#motionSecond').innerHTML=opts;if(r.scenes.length>1)$('#motionSecond').selectedIndex=1;open('#motionDialog');});bind('#motionCalculate',async()=>{await api('/api/motion',{first:$('#motionFirst').value,second:$('#motionSecond').value,preset:$('#preset').value,channel:9});S.busy=true;});
  bind('#pmcArchivePlan',planPmcArchive);bind('#pmcArchivePrepare',pmcPrepareArchive);bind('#pmcArchivePrepareResume',async()=>{const task=await api('/api/pmc/archive/prepare/state');await api('/api/pmc/archive/prepare/resume',{id:task.id});await pmcRefreshArchivePrepare();});bind('#pmcArchiveRun',pmcStartArchive);bind('#pmcArchiveResume',async()=>{const task=await api('/api/pmc/archive/state');await api('/api/pmc/archive/resume',{id:task.id});await pmcRefreshArchiveTask();});bind('#pmcArchiveCancel',async()=>{let task=await api('/api/pmc/archive/state');if(task.status!=='running')task=await api('/api/pmc/archive/prepare/state');await api('/api/pmc/archive/cancel',{id:task.id});await pmcRefreshArchiveTask();});
  window.addEventListener('pmc-credentials-saved',async event=>{const action=S.pmcPrepareNeedsAction;if(!action||event.detail?.source!==action.source)return;try{await api('/api/pmc/archive/prepare/resume',{id:action.id});S.pmcPrepareNeedsAction=null;await pmcRefreshArchivePrepare();}catch(e){pmcFeedback('Не удалось продолжить подготовку: '+e.message,'error-text');}});
 $('#pmcArchiveDialog').addEventListener('change',event=>{if(event.target.matches('#pmcArchiveStart,#pmcArchiveEnd,#pmcNorth,#pmcWest,#pmcSouth,#pmcEast,[name="pmcArchiveSource"]'))pmcInvalidateArchivePlan('Параметры изменились. Составьте план заново.');});
}
async function boot(){const key=location.hash.slice(1);if(key){await api('/api/bootstrap',{key});history.replaceState(null,'',location.pathname);}S.registry=await api('/api/registry');$('#month').innerHTML=months.map((m,i)=>`<option value="${i}">${m}</option>`).join('');$('#channel').innerHTML=S.registry.channels.map(c=>`<option value="${c.id}">${c.id} · ${c.wavelength_um} мкм · ${escape(c.name)}</option>`).join('');$('#channel').value=9;$('#product').innerHTML=S.registry.products.filter(p=>!['planned','profile_tool'].includes(p.status)&&p.id!=='motion').map(p=>`<option value="${p.id}">${escape(p.title)}</option>`).join('');$('#preset').innerHTML=Object.entries(S.registry.presets).map(([id,p])=>`<option value="${id}">${escape(p.name)}</option>`).join('');registerEvents();await setMap('arctic');const state=await api('/api/state');S.serverState=state;const saved=state.view_settings||{};if(saved.product)$('#product').value=saved.product;if(saved.channel)$('#channel').value=saved.channel;if(saved.preset){$('#preset').value=saved.preset;await setMap(saved.preset);}setDate(state.last_date||S.day);productHint();poll();}
