/* Продукт выбирает пользователь; каналы, ожидание и шкалы проверяет приложение. */
'use strict';
const PRODUCT_FLOW={serial:0, pending:null, awaitingTime:null, height:false, inventory:null, scaleScene:null, scaleRevision:0};
const sleepFlow=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function flowMessage(text,action,label){
  const el=$('#productFeedback');if(!el)return;
  el.replaceChildren();const p=document.createElement('p');p.textContent=text;el.append(p);el.hidden=!text;
  if(action){const button=document.createElement('button');button.className='tonal full';button.textContent=label;button.onclick=()=>Promise.resolve(action()).catch(e=>flowMessage(e.message));el.append(button);}
}
function cancelProductFlow(){PRODUCT_FLOW.serial++;PRODUCT_FLOW.pending=null;PRODUCT_FLOW.height=false;}
function requestBuild(){
  const wanted=PRODUCT_FLOW.awaitingTime||{product:$('#product').value,channel:Number($('#channel').value)||9};
  PRODUCT_FLOW.awaitingTime=null;
  cancelProductFlow();cancelCatalogOpen();clearTimeout(UI.buildTimer);UI.wantedBuild=null;
  UI.contextEpoch++;UI.mapSequence++;invalidatePoint();invalidateRoute();
  if(!S.scene){
    PRODUCT_FLOW.awaitingTime=wanted;left('catalog');$('#catalogCalendar').open=true;
    flowMessage('Сначала выберите срок наблюдения. Выбранный продукт сохранён.');toast('Выберите дату и срок для продукта.');return;
  }
  const task={...wanted,serial:PRODUCT_FLOW.serial,platform:S.scene.platform,time:S.scene.time,preset:$('#preset').value,composite:wanted.product==='archive_rgb'?UI.compositeId:undefined};
  PRODUCT_FLOW.pending=task;
  runProductFlow(task).catch(e=>{if(task.serial===PRODUCT_FLOW.serial){PRODUCT_FLOW.pending=null;flowMessage(e.message,requestBuild,'Повторить');}});
}
function flowCurrent(task){return task.serial===PRODUCT_FLOW.serial&&S.scene?.platform===task.platform&&S.scene?.time===task.time;}
async function runProductFlow(task){
  let searched=false;const queued=new Set();
  while(flowCurrent(task)){
    if(S.busy||UI.activeBuild){flowMessage('Ожидание завершения текущей операции…');await sleepFlow(400);continue;}
    const plan=await api('/api/product-plan',task);if(!flowCurrent(task))return;
    PRODUCT_FLOW.inventory=plan;productHint();
    if(plan.status==='catalog'){
      if(searched){PRODUCT_FLOW.pending=null;flowMessage('В каталоге этого срока не найдены каналы '+plan.absent.join(', ')+'. Выберите другой срок.',()=>left('catalog'),'Выбрать срок');return;}
      searched=true;flowMessage('Ищу необходимые каналы за '+task.time.slice(0,10)+'…');
      await api('/api/search',{date:task.time.slice(0,10),platform:task.platform,scope:'day'});
      while(flowCurrent(task)){
        const state=await api('/api/state');if(!state.busy){
          const errors=Array.isArray(state.result?.result)?state.result.result.filter(r=>r.error):[];
          if(errors.length)throw Error(errors.map(r=>r.error).join('; '));break;
        }
        await sleepFlow(600);
      }
      if(!flowCurrent(task))return;await loadDay();continue;
    }
    if(plan.status==='download'){
      if(plan.download_ids.some(id=>queued.has(id)))throw Error('Загрузка остановлена или завершилась ошибкой. Проверьте очередь и повторите.');
      if(plan.bytes>=512*1024*1024&&!await ask('Скачать данные для продукта',plan.download_ids.length+' файлов · '+size(plan.bytes)+(plan.unknown_sizes?' и файлы неизвестного размера':''))){PRODUCT_FLOW.pending=null;flowMessage('Загрузка отменена.');return;}
      if(!flowCurrent(task))return;
      flowMessage('Скачиваю нужные каналы: '+plan.missing.join(', ')+'. Продукт откроется после загрузки.');
      await api('/api/queue',{ids:plan.download_ids});plan.download_ids.forEach(id=>queued.add(id));await sleepFlow(500);continue;
    }
    if(plan.status==='waiting'){flowMessage('Каналы '+plan.waiting.join(', ')+' загружаются. Можно продолжать работу с картой.');await sleepFlow(800);continue;}
    if(plan.status==='blocked')throw Error('Скачанные каналы '+plan.blocked.join(', ')+' не распознаны. Откройте список файлов для причины.');
    if(plan.status==='unavailable')throw Error('Готовой RGB-композиции этого срока нет. Выберите спектральный продукт или другой срок.');
    if(plan.status==='calibration'){
      PRODUCT_FLOW.pending=null;
      left('product');
      flowMessage('Каналы готовы. В файлах '+plan.calibration_needed.join(', ')+' не объявлена температурная шкала.',()=>showScaleDialog(plan.calibration_needed),'Настроить шкалу и продолжить');return;
    }
    if(plan.status!=='ready')throw Error(plan.message||'Выберите срок.');
    await loadDay();if(!flowCurrent(task))return;
    if(plan.product==='water'){
      PRODUCT_FLOW.pending=null;showProfiles();flowMessage('Влагосодержание рассчитывается из профиля давления и влажности, не из цвета снимка.');return;
    }
    PRODUCT_FLOW.pending=null;PRODUCT_FLOW.height=plan.product==='cth';
    $('#product').value=plan.display_product;$('#channel').value=PRODUCT_FLOW.height?'9':String(task.channel);
    if(plan.composite)UI.compositeId=plan.composite;
    productHint();flowMessage(PRODUCT_FLOW.height?'Открою оконный канал. Затем выберите облачную точку и атмосферный профиль.':'Каналы готовы. Построение продукта…');
    submitProductBuild();return;
  }
}
function productFlowShown(product){
  if(PRODUCT_FLOW.height&&product.product==='channel'){
    $('#product').value='cth';productHint();
    flowMessage('Выберите облако на карте. Для высоты нужен профиль T(z); облачность и непрозрачность подтверждаются в точке.',()=>showProfiles(),'Выбрать профиль');
    tab('point');
  }else if(!PRODUCT_FLOW.pending){flowMessage('');}
}
function syncProductChoices(){
  if(!S.registry)return;
  const have=new Set((S.scene?.channels||[]).map(c=>c.channel));
  for(const option of $$('#product option'))option.disabled=false;
  for(const option of $$('#channel option'))option.disabled=false;
  for(const button of $$('#taskGrid button')){
    button.disabled=false;const id=button.dataset.task,spec=S.registry.products.find(p=>p.id===id);
    const need=id==='channel'?[9]:id==='cth'?[7,9,10]:spec?.channels||[];
    const missing=need.filter(c=>!have.has(c));
    const state=!S.scene?'Выбрать срок':id==='motion'?'Слежение по двум срокам':missing.length?'Нужные каналы будут найдены и скачаны':'Каналы на диске';
    button.title=state;
    let tag=button.querySelector('.availability-tag');if(!tag){tag=document.createElement('small');tag.className='availability-tag';button.append(tag);}tag.textContent=state;
  }
}
function scaleData(){
  const method=$('#calMode').value;
  return {scene:PRODUCT_FLOW.scaleScene,method:({unknown:'auto',assumed:'linear',declared:'declared',anchors:'two_anchors',matched:'matched_pairs'})[method],
    scale:$('#scaleA').value,offset:$('#scaleB').value,dn1:$('#scaleDN1').value,t1:$('#scaleT1').value,
    dn2:$('#scaleDN2').value,t2:$('#scaleT2').value,text:$('#scalePairs').value,reference:$('#scaleReference').value,
    product:$('#product').value,channel:Number($('#channel').value)||9,preset:$('#preset').value,channels:$$('#scaleChannels input:checked').map(e=>Number(e.value)),scope:$('#scaleScope').value};
}
function scaleError(text){$('#scaleError').textContent=text;$('#scaleError').hidden=!text;}
function scaleMethod(){
  const mode=$('#calMode').value;
  $('#scaleLinear').hidden=!['assumed','declared'].includes(mode);$('#scaleAnchors').hidden=mode!=='anchors';$('#scaleMatched').hidden=mode!=='matched';
  $('#scaleReference').closest('label').hidden=mode==='unknown';$('#scaleCaution').hidden=mode==='unknown'||mode==='declared';
  $('#scaleSliders').hidden=mode!=='assumed';$('#scalePreview').replaceChildren();scaleError('');
  previewScale();
}
async function previewScale(){
  const seq=++PRODUCT_FLOW.scaleRevision,data=scaleData();$('#scaleImageResult')?.replaceChildren();
  if(data.method==='auto'){scaleError('');$('#scalePreview').textContent='Будут использованы только явно объявленные единицы GeoTIFF/STAC. Файлы без единиц останутся DN.';return;}
  try{
    const proposal=await api('/api/scale/preview',data);if(seq!==PRODUCT_FLOW.scaleRevision)return;scaleError('');
    const selected=data.channels;const values=(PRODUCT_FLOW.scaleInventory?.channels||[]).filter(c=>selected.includes(c.channel));
    let html=`<strong>Tя = ${num(proposal.scale,6)} × DN ${proposal.offset<0?'−':'+'} ${num(Math.abs(proposal.offset),4)} K</strong>`;
    if(proposal.rmse_fit_k!==undefined)html+=`<p>Пар: ${proposal.n}. RMSE подгонки: ${num(proposal.rmse_fit_k,3)} K${proposal.rmse_group_cv_k!==undefined?'; контроль по исключаемым группам: '+num(proposal.rmse_group_cv_k,3)+' K':''}. Это не независимая оценка точности.</p>`;
    const samples=values.flatMap(c=>c.sample_dn||[]);if(samples.length){
      const lo=Math.min(...samples),hi=Math.max(...samples),ys=samples.map(x=>proposal.scale*x+proposal.offset-273.15);
      const ylo=Math.min(...ys),yhi=Math.max(...ys),x=v=>32+236*(v-lo)/(hi-lo||1),y=v=>134-100*(v-ylo)/(yhi-ylo||1);
      html+=`<svg class="scale-plot" viewBox="0 0 300 164" role="img" aria-label="Преобразование хранимых значений в температуру"><line x1="32" y1="134" x2="268" y2="134" stroke="currentColor"/><path d="M32,${y(proposal.scale*lo+proposal.offset-273.15)} L268,${y(proposal.scale*hi+proposal.offset-273.15)}" fill="none" stroke="#3159c5" stroke-width="2"/><text x="32" y="154">${num(lo)} DN</text><text x="235" y="154">${num(hi)}</text><text x="32" y="16">${num(ylo)}…${num(yhi)} °C · выбранные каналы</text></svg>`;
      html+='<table><thead><tr><th>Канал</th><th>DN, выборка 2–98%</th><th>После, °C</th></tr></thead><tbody>'+values.map(c=>{const a=c.sample_dn?.[1],b=c.sample_dn?.[5];return `<tr><td>${c.channel}</td><td>${num(a)}…${num(b)}</td><td>${num(a*proposal.scale+proposal.offset-273.15)}…${num(b*proposal.scale+proposal.offset-273.15)}</td></tr>`;}).join('')+'</tbody></table>';
      if(ys.some(v=>v<120-273.15||v>400-273.15))html+='<p class="error-text">Часть выборки выходит за 120–400 K и будет исключена из физических продуктов.</p>';
      if(proposal.valid_dn&&samples.some(x=>x<proposal.valid_dn[0]||x>proposal.valid_dn[1]))html+='<p class="notice">В данных есть значения за пределами опор. Вне диапазона калибровка не проверена.</p>';
    }
    $('#scalePreview').innerHTML=html;
  }catch(e){if(seq===PRODUCT_FLOW.scaleRevision){$('#scalePreview').replaceChildren();scaleError(e.message);}}
}
async function showScaleDialog(required){
  if(!S.scene){left('catalog');toast('Выберите срок наблюдения.');return;}
  const serial=++PRODUCT_FLOW.scaleRevision,scene=S.scene.id;
  const inv=await api('/api/scale/inventory',{scene});if(serial!==PRODUCT_FLOW.scaleRevision||S.scene?.id!==scene)return;
  PRODUCT_FLOW.scaleScene=scene;PRODUCT_FLOW.scaleInventory=inv;
  if(!required?.length){
    const id=$('#product').value,spec=S.registry.products.find(p=>p.id===id);
    required=id==='cth'?[7,9,10]:id==='channel'?[Number($('#channel').value)]:(spec?.channels||[]);
  }
  required=required.filter(ch=>inv.channels.some(c=>c.channel===ch));
  if(!required.length&&inv.channels.length)required=[inv.channels[0].channel];
  const dialog=$('#calibrationDialog');dialog.querySelector('.dialog-head h2').textContent='Температурная шкала';
  const body=dialog.querySelector('.dialog-body');body.innerHTML=`<p class="hint">${escape(inv.platform+' · '+inv.time.replace('T',' ').replace('Z',' UTC'))}</p><fieldset id="scaleChannels"><legend>Каналы с общей настройкой</legend>${inv.channels.map(c=>`<label class="check"><input type="checkbox" value="${c.channel}" ${(required?.length?required.includes(c.channel):c.channel===Number($('#channel').value))?'checked':''}>${c.channel} · ${escape(c.scale?.status==='metadata'?'шкала из метаданных':c.scale?.status==='assumed'?'исследовательская шкала':c.scale?.status==='declared'?'заданы коэффициенты':'единицы не объявлены')}</label>`).join('')}</fieldset><button id="scaleAll" class="text-button">Выбрать все ИК-каналы</button><label>Способ<select id="calMode"><option value="unknown">Автоматически из метаданных</option><option value="assumed">Исследовательская линейная шкала</option><option value="anchors">По двум опорным температурам</option><option value="matched">По сопоставленным измерениям CSV</option><option value="declared">Коэффициенты из документа поставщика</option></select></label><p id="scaleCaution" class="notice" hidden>Это исследовательское приближение. Необъявленные DN нельзя признать кельвинами по внешнему виду снимка. Шкала применяется только к отмеченным каналам.</p><div id="scaleLinear" hidden><div class="row"><label>Масштаб a<input id="scaleA" type="number" step="any" value="1"></label><label>Сдвиг b, K<input id="scaleB" type="number" step="any" value="0"></label></div><div id="scaleSliders"><label>Масштаб<input id="scaleARange" type="range" min="0.01" max="2" step="0.01" value="1"></label><label>Сдвиг<input id="scaleBRange" type="range" min="-100" max="100" step="0.5" value="0"></label></div><p class="micro">Начальное a=1, b=0 — проверяемое допущение DN=K, не найденные коэффициенты прибора.</p></div><div id="scaleAnchors" hidden><div class="row"><label>DN первой опоры<input id="scaleDN1" type="number" step="any"></label><label>Её Tя, K<input id="scaleT1" type="number" step="any"></label></div><div class="row"><label>DN второй опоры<input id="scaleDN2" type="number" step="any"></label><label>Её Tя, K<input id="scaleT2" type="number" step="any"></label></div><p class="micro">Пример арифметики, не измерение: 100 → 220 K и 500 → 300 K дают a=0,2 и b=200 K. Опоры берите из сопоставленного канала, не из температуры воздуха у земли.</p></div><div id="scaleMatched" hidden><label>Сопоставленные пары<input id="scaleFile" type="file" accept=".csv,text/csv"></label><label>dn,temperature_k (или temperature_c), необязательно group<textarea id="scalePairs" rows="4" placeholder="dn,temperature_k,group"></textarea></label><p class="micro">Не менее 6 пар. group — независимый участок или срок; при 3 группах дополнительно проверяется перенос на исключённую группу.</p></div><label>Источник коэффициентов или опор<input id="scaleReference" placeholder="Прибор, канал, срок, документ / метод сопоставления"></label><label>Применение<select id="scaleScope"><option value="scene">Только выбранный срок</option><option value="platform">Этот аппарат, в том числе последующие сроки</option></select></label><p id="scaleScopeNote" class="notice" hidden>Межсрочная стабильность не подтверждена. Коэффициенты другого аппарата не используются.</p><p id="scaleError" class="error-text" role="alert" hidden></p><div id="scalePreview" aria-live="polite"></div><button id="scaleImageButton" class="tonal full">Посмотреть продукт без сохранения</button><div id="scaleImageResult"></div><a class="export-link" href="/docs/CALIBRATION.html" target="_blank" rel="noopener">Как выбрать опоры и проверить результат</a>`;
  let footer=dialog.querySelector('.dialog-footer');if(!footer){footer=document.createElement('div');footer.className='dialog-footer';dialog.append(footer);}
  footer.innerHTML='<button id="saveCalibration" class="primary full">Применить и пересчитать</button>';
  const selectedScales=inv.channels.filter(c=>required.includes(c.channel)).map(c=>c.scale);
  const firstScale=selectedScales[0];
  const current=firstScale&&selectedScales.every(c=>c&&c.scale===firstScale.scale&&c.offset===firstScale.offset&&c.status===firstScale.status)?firstScale:null;
  if(current){$('#scaleA').value=current.scale;$('#scaleB').value=current.offset;$('#scaleReference').value=current.reference||'';}
  $('#calMode').value=current?.status==='assumed'?'assumed':current?.status==='declared'?'declared':'unknown';
  $('#scaleAll').onclick=()=>{$$('#scaleChannels input').forEach(e=>e.checked=true);previewScale();};
  $('#calMode').onchange=scaleMethod;$('#scaleScope').onchange=()=>$('#scaleScopeNote').hidden=$('#scaleScope').value!=='platform';
  for(const id of ['#scaleARange','#scaleBRange'])$(id).oninput=()=>{$(id==='#scaleARange'?'#scaleA':'#scaleB').value=$(id).value;previewScale();};
  let timer;for(const e of [...body.querySelectorAll('input:not([type=file]):not([type=range]),textarea')])e.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(previewScale,180);});
  $('#scaleFile').onchange=async()=>{const f=$('#scaleFile').files[0];if(!f)return;if(f.size>1_000_000){scaleError('CSV больше 1 МБ.');return;}$('#scalePairs').value=await f.text();previewScale();};
  $('#scaleImageButton').onclick=async()=>{
    const revision=PRODUCT_FLOW.scaleRevision;$('#scaleImageButton').disabled=true;scaleError('');
    try{const result=await api('/api/scale/image',scaleData());if(revision!==PRODUCT_FLOW.scaleRevision)return;const image=document.createElement('img');image.src=result.image;image.alt=result.title;image.style.cssText='width:100%;max-height:260px;object-fit:contain';const note=document.createElement('p');note.textContent=result.message+' '+(result.status==='assumed'?'Исследовательская шкала.':'');$('#scaleImageResult').replaceChildren(image,note);}
    catch(e){scaleError(e.message);}finally{if($('#scaleImageButton'))$('#scaleImageButton').disabled=false;}
  };
  $('#saveCalibration').onclick=async()=>{
    const data=scaleData();scaleError('');if(!data.channels.length){scaleError('Выберите хотя бы один ИК-канал.');return;}$('#saveCalibration').disabled=true;
    try{
      const proposal=await api('/api/scale/preview',data);
      if(proposal.status==='assumed'&&!await ask('Исследовательская шкала','Каналы '+data.channels.join(', ')+'. Настройка будет помечена как допущение во всех новых результатах. Применить?'))return;
      if(S.scene?.id!==scene)throw Error('Срок изменился. Откройте настройку заново.');
      await api('/api/scale/save',{...data,acknowledged:true});dialog.close();PRODUCT_FLOW.inventory=null;requestBuild();
    }catch(e){scaleError(e.message);}finally{if($('#saveCalibration'))$('#saveCalibration').disabled=false;}
  };
  if(!dialog.open)dialog.showModal();scaleMethod();
}
function installProductFlow(){
  // A remembered product owns the primary time-selection action, not the catalog's previous bundle.
  $('#sessions').addEventListener('click',event=>{
    if(!PRODUCT_FLOW.awaitingTime)return;
    const button=event.target.closest('button.session');if(!button)return;
    const row=CATALOG.rows.find(r=>sessionKey(r)===button.dataset.key);if(!row)return;
    event.preventDefault();event.stopImmediatePropagation();
    const scene=S.scenes.find(s=>s.id===row.local_id)||{id:row.platform+'_'+row.time.replace(/[-:TZ]/g,''),platform:row.platform,time:row.time,channels:[],composites:[]};
    selectScene(scene);left('product');
  },true);
  const feedback=document.createElement('section');feedback.id='productFeedback';feedback.className='product-feedback';feedback.setAttribute('role','status');feedback.hidden=true;$('#taskGrid').after(feedback);
  const height=document.createElement('option');height.value='cth';height.textContent='Высота облака в точке · по профилю';$('#product').append(height);
  const button=$('#calibrationOpen'),replacement=button.cloneNode(true);button.replaceWith(replacement);replacement.onclick=()=>showScaleDialog().catch(e=>flowMessage(e.message));
  $('#process').textContent='Открыть выбранный продукт';
  const style=document.createElement('style');style.textContent='.availability-tag{font-size:10px;color:var(--muted);line-height:1.4}.product-feedback{background:var(--tonal);padding:12px;border-radius:12px;margin:12px 0;font-size:12px}.product-feedback p{margin:0}.scale-plot{width:100%;height:150px}.scale-plot text{font-size:10px;fill:currentColor}#scaleChannels{border:1px solid var(--line);border-radius:10px}#scaleChannels legend{font-size:12px}#scaleChannels .check{margin:6px 0}#scaleSliders input{padding:0}#scalePreview{font-size:12px;margin:14px 0}#scalePreview td{font-size:11px}';document.head.append(style);
}
