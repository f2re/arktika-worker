/* ERA5: секреты только в памяти сервера; задача и результат привязаны к сроку. */
'use strict';
const ERA5_UI={scene:null,channels:[],requestSerial:0,report:null,timer:null};
const era5Dialog=document.createElement('dialog');era5Dialog.id='era5Dialog';era5Dialog.className='wide';
era5Dialog.innerHTML=`<div class="dialog-head"><div><h2>Автокалибровка по ERA5</h2><p id="era5Scene"></p></div><button data-close aria-label="Закрыть автокалибровку">✕</button></div>
<div class="dialog-body"><p class="notice">Спектральный аналог — МСУ-ГС «Электро-Л» №2. Результат исследовательский, не калибровка «Арктики» поставщиком.</p>
<section><h3>1. Доступ к ERA5</h3><label>Источник<select id="era5Provider"><option value="cds">Copernicus CDS</option><option value="gdex">NCAR / GDEX</option></select></label>
<label>Файл доступа .netrc или .cdsapirc<button id="era5ChooseFile" class="tonal" type="button">Выбрать файл доступа</button><input id="era5CredentialFile" class="visually-hidden" type="file" accept=".netrc,.cdsapirc,text/plain" tabindex="-1"></label>
<p class="micro">CDS: персональный токен, не пароль NCAR. В .netrc для CDS токен находится в password записи cds.climate.copernicus.eu. Запись NCAR автоматически выберет GDEX.</p>
<details><summary>Ввести токен CDS вместо файла</summary><label>Персональный токен<input id="era5Token" type="password" autocomplete="off"></label><button id="era5SaveToken" class="tonal">Подключить CDS</button></details>
<div class="row"><span id="era5CredentialStatus" class="micro"></span><button id="era5ClearCredential" class="text-button">Удалить доступ</button></div></section>
<section><h3>2. Опорная область</h3><p id="era5Channels" class="hint"></p><p class="micro">Начальная область — Баренцево / Норвежское моря. Выбираются открытая вода и малое покрытие облаками ERA5, а не холодные пиксели изображения.</p>
<div class="row"><label>Север, °<input id="era5North" type="number" step="0.25" value="80"></label><label>Юг, °<input id="era5South" type="number" step="0.25" value="60"></label></div>
<div class="row"><label>Запад, °<input id="era5West" type="number" step="0.25" value="0"></label><label>Восток, °<input id="era5East" type="number" step="0.25" value="40"></label></div>
<button id="era5CheckPlan" class="tonal">Проверить состав запроса</button><div id="era5Plan" class="hint" aria-live="polite"></div></section>
<section><h3>3. Расчёт опорного сигнала</h3><p id="era5Runtime" class="hint"></p><button id="era5GetCoefficients" class="tonal">Загрузить коэффициенты Электро-Л №2</button>
<p class="micro">RTTOV 13.2 устанавливается один раз. Загрузка ERA5 не требует RTTOV. Архив коэффициентов ограничен 1 ГиБ; сохраняется только таблица Электро-Л.</p>
<label>Зенитный угол наблюдения, °<input id="era5Zenith" type="number" min="0" max="70" step="0.1" placeholder="Из геометрии спутника"></label>
<label class="check"><input id="era5GeometryAck" type="checkbox">Для этой небольшой области принимаю указанный угол постоянным.</label>
<label class="check"><input id="era5ResearchAck" type="checkbox">Использовать исследовательский аналог Электро-Л; за диапазоном опор температуры не вычислять.</label>
<p class="micro">Угол не определяется из температуры. Без этих подтверждений можно только синхронизировать ERA5. На текущие сроки ERA5 обычно ещё не опубликована.</p></section>
<p id="era5Error" class="error-text" role="alert" hidden></p><p id="era5Progress" role="status" aria-live="polite"></p><div id="era5Report"></div>
<a class="export-link" href="/docs/ERA5.html" target="_blank" rel="noopener">Установка, доступ и методика</a></div>
<div class="dialog-footer"><button id="era5Cancel" hidden>Остановить</button><button id="era5Sync" class="tonal">Только загрузить ERA5</button><button id="era5Run" class="primary">Рассчитать шкалу</button></div>`;
document.body.append(era5Dialog);
function era5Error(text=''){const e=$('#era5Error');e.textContent=text;e.hidden=!text;}
function era5Input(){
  if(!ERA5_UI.scene||S.scene?.id!==ERA5_UI.scene)throw Error('Выбранный срок изменился. Откройте автокалибровку заново.');
  return {scene:ERA5_UI.scene,channels:ERA5_UI.channels,provider:$('#era5Provider').value,
    area:['#era5North','#era5West','#era5South','#era5East'].map(id=>$(id).value===''?null:Number($(id).value)),
    zenith_deg:$('#era5Zenith').value===''?null:Number($('#era5Zenith').value),
    constant_angle_acknowledged:$('#era5GeometryAck').checked,acknowledged:$('#era5ResearchAck').checked,
    allow_coefficient_download:true};
}
async function era5Refresh(){
  const state=await api('/api/era5/state');
  const c=state.credentials;$('#era5CredentialStatus').textContent=c.present?(c.provider==='cds'?'CDS':'GDEX')+' · доступ в памяти до остановки сервера':'Доступ не задан. GDEX сначала запрашивается публично.';
  const r=state.runtime;$('#era5Runtime').textContent=[r.pyrttov?'Обёртка RTTOV найдена':'RTTOV 13.2 не установлен',r.coefficient.present?'Коэффициенты Электро-Л загружены':'Таблица RTTOV ещё не загружена',r.data_dependencies_missing.length?'Нет зависимостей ERA5: '+r.data_dependencies_missing.join(', '):'Библиотеки ERA5 готовы'].join('. ')+'.';
  $('#era5Run').disabled=state.busy;$('#era5Sync').disabled=state.busy;$('#era5GetCoefficients').disabled=state.busy||r.coefficient.present;
  $('#era5Cancel').hidden=!state.busy;$('#era5ClearCredential').disabled=state.busy;
  const j=state.job;$('#era5Progress').textContent=state.busy?(j.status==='running'?j.phase:state.operation):(j.status==='idle'?'':j.phase||j.status);
  if(j.id&&j.status!=='running'&&j.scene===ERA5_UI.scene&&ERA5_UI.report?.id!==j.id){
    ERA5_UI.report=await api('/api/era5/report?'+qs({id:j.id}));era5DrawReport(ERA5_UI.report);
  }
  return state;
}
async function showEra5Dialog(required){
  if(!S.scene){left('catalog');toast('Выберите срок наблюдения.');return;}
  ERA5_UI.scene=S.scene.id;ERA5_UI.report=null;$('#era5Report').replaceChildren();era5Error();
  const inv=await api('/api/scale/inventory',{scene:ERA5_UI.scene});
  ERA5_UI.channels=(required||[]).filter(c=>inv.channels.some(i=>i.channel===c));
  if(!ERA5_UI.channels.length){
    const spec=S.registry.products.find(p=>p.id===$('#product').value);
    const needed=$('#product').value==='cth'?[7,9,10]:$('#product').value==='channel'?[Number($('#channel').value)]:(spec?.channels||[]);
    ERA5_UI.channels=needed.filter(c=>inv.channels.some(i=>i.channel===c));
  }
  if(!ERA5_UI.channels.length)throw Error('Для автокалибровки выберите продукт со скачанными ИК-каналами 4–10.');
  $('#era5Scene').textContent=(inv.platform==='ARCM1'?'Арктика-М1':inv.platform==='ARCM2'?'Арктика-М2':inv.platform)+' · '+inv.time.replace('T',' ').replace('Z',' UTC');
  $('#era5Channels').textContent='Каналы выбранного продукта: '+ERA5_UI.channels.join(', ')+'. Коэффициенты рассчитываются для каждого отдельно.';
  $('#era5Plan').replaceChildren();$('#era5GeometryAck').checked=false;$('#era5ResearchAck').checked=false;
  if($('#calibrationDialog').open)$('#calibrationDialog').close();
  era5Dialog.showModal();
  const state=await era5Refresh();if(state.credentials.provider)$('#era5Provider').value=state.credentials.provider;
  clearInterval(ERA5_UI.timer);ERA5_UI.timer=setInterval(()=>{if(era5Dialog.open)era5Refresh().catch(e=>era5Error(e.message));},1500);
}
function era5DrawReport(report){
  let html='<h3>Результат</h3><p class="hint">'+escape(report.message||'')+'</p>';
  if(report.n_references!==undefined)html+='<p class="hint">Принято опор: '+report.n_references+'. Это модельные опоры, не эталонные измерения.</p>';
  const passed=[];
  for(const [ch,row] of Object.entries(report.channels||{})){
    if(row.status==='passed'){
      const p=row.proposal;passed.push(Number(ch));html+=`<article class="reference-card"><h3>Канал ${escape(ch)} · прошёл внутреннюю проверку</h3><p>Tя = ${num(p.scale,6)} × DN + ${num(p.offset,3)} K</p><p>DN ${p.valid_dn.map(v=>num(v,2)).join('…')} · Tя ${p.valid_temperature_k.map(v=>num(v-273.15,1)).join('…')} °C</p><p>Групповое расхождение ${num(p.group_cv_rmse_k,2)} K · ${p.n} опор / ${p.groups} групп. Не полная погрешность прибора.</p></article>`;
    }else html+=`<article class="reference-card"><h3>Канал ${escape(ch)} · не применяется</h3><p>${escape(row.reason)}</p></article>`;
  }
  if(report.assumptions)html+='<details><summary>Допущения и границы</summary>'+report.assumptions.map(t=>'<p class="micro">'+escape(t)+'</p>').join('')+'</details>';
  html+=`<a class="export-link" href="/api/era5/report?${qs({id:report.id})}" download="era5-calibration.json">Сохранить отчёт и происхождение опор</a>`;
  if(passed.length)html+='<button id="era5Apply" class="primary full">Применить к этому сроку и открыть продукт</button>';
  $('#era5Report').innerHTML=html;
  if(passed.length)$('#era5Apply').onclick=async()=>{
    era5Error();try{
      era5Input();if(!await ask('Применить модельно-опорную шкалу','Только каналы '+passed.join(', ')+', только этот срок. Вне проверенного диапазона пиксели будут скрыты. Спектральный аналог Электро-Л не является калибровкой Арктики поставщиком.'))return;
      await api('/api/era5/apply',{id:report.id,scene:ERA5_UI.scene,channels:passed,acknowledged:true});era5Dialog.close();requestBuild();
    }catch(e){era5Error(e.message);}
  };
}
$('#era5ChooseFile').onclick=()=>$('#era5CredentialFile').click();
$('#era5CredentialFile').onchange=async()=>{
  const sequence=++ERA5_UI.requestSerial;const f=$('#era5CredentialFile').files[0];$('#era5CredentialFile').value='';if(!f)return;
  era5Error();try{
    if(f.size>65536)throw Error('Файл доступа больше 64 КиБ.');const text=await f.text();if(sequence!==ERA5_UI.requestSerial)return;
    const r=await api('/api/era5/credentials',{text,format:'auto'});if(sequence!==ERA5_UI.requestSerial)return;
    $('#era5Provider').value=r.provider;await era5Refresh();
  }catch(e){era5Error(e.message);}
};
$('#era5SaveToken').onclick=async()=>{era5Error();const text=$('#era5Token').value;$('#era5Token').value='';try{await api('/api/era5/credentials',{text,format:'token'});$('#era5Provider').value='cds';await era5Refresh();}catch(e){era5Error(e.message);}};
$('#era5ClearCredential').onclick=async()=>{era5Error();try{++ERA5_UI.requestSerial;await api('/api/era5/credentials',{clear:true});await era5Refresh();}catch(e){era5Error(e.message);}};
$('#era5CheckPlan').onclick=async()=>{era5Error();try{const p=await api('/api/era5/plan',era5Input());$('#era5Plan').textContent=p.requests.length+' подзапроса; '+p.times.join(' / ')+'. Температура, влажность, озон и геопотенциал на 37 уровнях; поверхность и маски. '+p.notes.join(' ');}catch(e){era5Error(e.message);}};
async function era5Start(dataOnly){era5Error();try{const input=era5Input();await api('/api/era5/start',{...input,data_only:dataOnly});ERA5_UI.report=null;$('#era5Report').replaceChildren();await era5Refresh();}catch(e){era5Error(e.message);}}
$('#era5Sync').onclick=()=>era5Start(true);$('#era5Run').onclick=()=>era5Start(false);
$('#era5GetCoefficients').onclick=async()=>{era5Error();try{if(!await ask('Получить таблицу Электро-Л №2','Будет прочитан официальный архив NWP SAF (лимит 1 ГиБ). Сохраняется только файл коэффициентов RTTOV.'))return;await api('/api/era5/coefficients',{acknowledged:true});await era5Refresh();}catch(e){era5Error(e.message);}};
$('#era5Cancel').onclick=async()=>{try{await api('/api/cancel',{});await era5Refresh();}catch(e){era5Error(e.message);}};
era5Dialog.addEventListener('close',()=>{clearInterval(ERA5_UI.timer);$('#era5Token').value='';});
const manualScaleDialog=showScaleDialog;
showScaleDialog=async function(required){
  await manualScaleDialog(required);
  if(!$('#calibrationDialog').open)return;
  const body=$('#calibrationDialog .dialog-body');const section=document.createElement('section');
  section.innerHTML='<button id="era5Open" class="tonal full">Автоматически подобрать шкалу по ERA5</button><p class="micro">Загрузка профилей и расчёт сигнала через RTTOV / Электро-Л №2. Ручная настройка — ниже.</p>';
  body.prepend(section);$('#era5Open').onclick=()=>showEra5Dialog($$('#scaleChannels input:checked').map(e=>Number(e.value))).catch(e=>scaleError(e.message));
};
