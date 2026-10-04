/* Один сценарий подготовки, привязанный к аппарату, сроку и продукту. */
'use strict';
const ERA5_UI={scene:null,channels:[],task:null,requestSerial:0,generation:0,report:null,timer:null,refreshing:false,state:null,preflight:null};
const era5Dialog=document.createElement('dialog');era5Dialog.id='era5Dialog';era5Dialog.className='wide fixed-dialog era5-dialog';
era5Dialog.innerHTML=`<div class="dialog-head era5-head"><div class="era5-heading"><div><h2>Подготовка по ERA5</h2><p id="era5Scene"></p></div><button data-close aria-label="Закрыть автокалибровку">✕</button></div>
<p id="era5Next" role="status">Проверяю готовность…</p><div class="era5-primary"><button id="era5Run" class="primary">Подготовить и рассчитать</button><button id="era5Cancel" hidden>Остановить</button></div><p class="micro">Исследовательская шкала по аналогу Электро-Л №2. Не калибровка поставщика.</p></div>
<div class="dialog-body"><ol id="era5Steps" class="era5-steps" aria-label="Этапы подготовки"></ol>
<p id="era5Error" class="error-text" role="alert" hidden></p>
<section id="era5Access"><h3>Подключить доступ</h3><p class="hint">Загрузите файл доступа или вставьте токен CDS — подготовка продолжится автоматически. Реквизиты останутся только в памяти до остановки сервера.</p>
<div class="row"><button id="era5ChooseFile" class="tonal">Выбрать .netrc / .cdsapirc</button><input id="era5CredentialFile" class="visually-hidden" type="file" tabindex="-1"></div>
<label>Токен Copernicus CDS<input id="era5Token" type="password" autocomplete="off" placeholder="Персональный токен"></label><button id="era5SaveToken" class="tonal">Подключить и продолжить</button>
<p class="micro"><a href="https://cds.climate.copernicus.eu/how-to-api" target="_blank" rel="noopener">Получить токен</a> · <a href="https://cds.climate.copernicus.eu/datasets/reanalysis-era5-pressure-levels?tab=download" target="_blank" rel="noopener">Условия атмосферных данных</a> · <a href="https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels?tab=download" target="_blank" rel="noopener">Условия поверхностных данных</a></p></section>
<section id="era5EngineHelp" hidden><h3>Подключить RTTOV 13.2</h3><p class="hint">Коэффициенты скачиваются автоматически и показаны отдельным этапом выше. Здесь требуется сама программа RTTOV — таблица её не заменяет. Получите дистрибутив NWP SAF, установите Python-обёртку и укажите папку один раз.</p>
<a class="text-button" href="https://nwp-saf.eumetsat.int/site/software/rttov/download/" target="_blank" rel="noopener">Получить RTTOV и принять лицензию</a>
<label>Папка установленного RTTOV<input id="era5EnginePath" placeholder="Например, /opt/rttov132"></label><button id="era5ConnectEngine" class="tonal">Проверить и подключить</button><p id="era5EngineMessage" class="hint"></p></section>
<section id="era5GeometryHelp" hidden><h3>Угол наблюдения</h3><p class="hint">В этих данных угол не указан. Введите его вручную или рассчитайте по актуальной орбите спутника (SGP4 / CelesTrak / SatNOGS).</p>
<div class="row" style="align-items:flex-end;gap:8px;"><label style="flex:1;">Зенитный угол, °<input id="era5Zenith" type="number" min="0" max="70" step="0.1" placeholder="Из геометрии наблюдения"></label><button id="era5CalcZenith" type="button" class="tonal" style="white-space:nowrap;margin-bottom:2px;">Рассчитать по орбите SGP4</button></div>
<p id="era5OrbitInfo" class="micro" style="margin-top:6px;color:#285a9e;"></p></section>
<div id="era5Report"></div>
<details id="era5Advanced"><summary>Область, источник и подробности</summary><p id="era5Channels" class="hint"></p><p class="micro">По умолчанию проверяется несколько морских акваторий. При сплошной облачности район переключается автоматически.</p>
<label>Выбор опорной акватории<select id="era5AreaPreset">
  <option value="auto">Автоматический выбор (чистое небо)</option>
  <option value="47,48,44,53">Каспийское море [47°N, 48°E, 44°N, 53°E]</option>
  <option value="68,0,62,10">Норвежское море (юг) [68°N, 0°E, 62°N, 10°E]</option>
  <option value="76,10,70,20">Норвежское море (север) [76°N, 10°E, 70°N, 20°E]</option>
  <option value="66,33,64,41">Белое море [66°N, 33°E, 64°N, 41°E]</option>
  <option value="60,2,55,8">Северное море [60°N, 2°E, 55°N, 8°E]</option>
  <option value="72,35,68,48">Баренцево море (юг) [72°N, 35°E, 68°N, 48°E]</option>
  <option value="56,145,51,153">Охотское море [56°N, 145°E, 51°N, 153°E]</option>
  <option value="custom">Свои координаты</option>
</select></label>
<div class="row"><label>Север, °<input id="era5North" type="number" step="0.25" value="76"></label><label>Юг, °<input id="era5South" type="number" step="0.25" value="70"></label></div>
<div class="row"><label>Запад, °<input id="era5West" type="number" step="0.25" value="10"></label><label>Восток, °<input id="era5East" type="number" step="0.25" value="20"></label></div>
<label>Источник<select id="era5Provider"><option value="cds">Copernicus CDS</option><option value="gdex">NCAR / GDEX</option></select></label><div class="row"><span id="era5CredentialStatus" class="micro"></span><button id="era5ClearCredential" class="text-button">Удалить доступ</button></div>
<button id="era5CheckPlan" class="text-button">Обновить проверку</button><p id="era5Plan" class="hint"></p><p id="era5Runtime" class="hint"></p><button id="era5Setup" class="tonal">Установить библиотеки сейчас</button>
<label class="check"><input id="era5TryRecent" type="checkbox">Проверить публикацию нового срока у источника, несмотря на обычную задержку.</label></details>
<p id="era5Progress" class="micro" aria-live="polite"></p></div>
<div class="dialog-footer"><a href="/docs/ERA5.html" target="_blank" rel="noopener">Как это работает</a><button id="era5Sync" class="tonal">Только загрузить ERA5</button></div>`;
document.body.append(era5Dialog);
era5Dialog.querySelector('[data-close]').onclick=()=>era5Dialog.close();
function era5Error(text=''){const e=$('#era5Error');e.textContent=text;e.hidden=!text;}
function era5Input(){
  if(!ERA5_UI.task)throw Error('Выберите срок наблюдения.');
  return {...ERA5_UI.task,channels:ERA5_UI.channels,provider:$('#era5Provider').value,
    area:['#era5North','#era5West','#era5South','#era5East'].map(id=>$(id).value===''?null:Number($(id).value)),
    zenith_deg:$('#era5Zenith').value===''?null:Number($('#era5Zenith').value),
    constant_angle_acknowledged:$('#era5Zenith').value!=='',acknowledged:true,allow_coefficient_download:true,
    try_recent:$('#era5TryRecent').checked};
}
function era5ContextCurrent(){return S.scene?.id===ERA5_UI.scene&&$('#product').value===ERA5_UI.task?.product&&(ERA5_UI.task.product!=='channel'||Number($('#channel').value)===ERA5_UI.task.channel);}
function era5ShowSteps(state,preflight){
  const r=state.runtime,j=state.job,own=j.scene===ERA5_UI.scene&&(!j.product||j.product===ERA5_UI.task?.product)||j.status==='setup_ready';
  const inventory=preflight?.inventory;
  const defaults=[
    {key:'dependencies',title:'Библиотеки',status:r.data_dependencies_missing.length?'pending':'done',message:r.data_dependencies_missing.length?'Установятся при запуске. Системный Python не изменится.':'Импорт библиотек проверен.'},
    {key:'channels',title:'Каналы снимка',status:!inventory||inventory.missing.length?'pending':'done',message:inventory?inventory.ready.length+'/'+ERA5_UI.channels.length+' готовы. '+(inventory.missing.length?'Остальные будут найдены и скачаны.':'Все нужные файлы прочитаны.'):'Проверка состава срока.'},
    {key:'era5',title:'Данные ERA5',status:preflight?.next_action==='archive'?'waiting':'pending',message:preflight?.next_action==='archive'?'Этот срок ещё не ожидается в архиве.':'Запрос, загрузка и проверка переменных, времени и сетки.'},
    {key:'coefficients',title:'Коэффициенты Электро-Л №2',status:r.coefficient.present?'done':'pending',message:r.coefficient.present?'Таблица проверена'+(r.coefficient.size_bytes?' · '+num(r.coefficient.size_bytes/1048576,2)+' МиБ':'')+'. Повторная загрузка не нужна.':'Скачаются автоматически, даже без программы RTTOV.'},
    {key:'engine',title:'Программа RTTOV и геометрия',status:r.pyrttov&&$('#era5Zenith').value!==''?'pending':'waiting',message:!r.pyrttov?'Нужно подключить RTTOV. Это не мешает подготовке данных.':$('#era5Zenith').value===''?'Нужен подтверждённый угол. Данные можно подготовить сейчас.':'Работоспособность программы проверит расчёт опор.'},
    {key:'calibration',title:'Шкала',status:'pending',message:'Расчёт опор и проверка диапазона; применение только после проверки.'}];
  const steps=defaults.map(d=>{
    if(d.key==='coefficients'&&r.coefficient.present)return d;
    const saved=own?(j.steps||[]).find(s=>s.key===d.key&&s.status!=='pending'):null;
    if(d.key==='coefficients'&&saved?.status==='done')return d; // Deleted file is not ready.
    return saved||d;
  });
  const labels={done:'Готово',running:'В работе',waiting:'Нужно действие',error:'Не завершено',pending:'Далее',skipped:'Не требуется',cancelled:'Остановлено'};
  $('#era5Steps').innerHTML=steps.map((s,i)=>`<li data-stage="${s.key}" data-status="${s.status}"><span class="era5-step-number">${s.status==='done'?'✓':i+1}</span><div><strong>${escape(s.title)}</strong><p>${escape(s.message||'')}</p></div><span class="era5-stage-status">${labels[s.status]||s.status}</span></li>`).join('');
}
async function era5Check(){
  const generation=ERA5_UI.generation;
  const result=await api('/api/era5/preflight',era5Input());
  if(generation!==ERA5_UI.generation)return;
  ERA5_UI.preflight=result;ERA5_UI.channels=result.plan.channels;
  $('#era5Channels').textContent='Каналы продукта: '+result.plan.channels.join(', ')+'. Проверяются файлы выбранного аппарата и срока.';
  $('#era5Plan').textContent=result.plan.requests.length+' подзапроса · '+result.plan.times.join(' / ')+'. Температура, влажность, озон и геопотенциал на 37 уровнях; поверхность и маски.';
  return result;
}
async function era5Refresh(){
  if(ERA5_UI.refreshing)return;
  ERA5_UI.refreshing=true;const generation=ERA5_UI.generation;
  try{
    const state=await api('/api/era5/state');if(generation!==ERA5_UI.generation)return;
    ERA5_UI.state=state;const c=state.credentials,r=state.runtime,j=state.job,p=ERA5_UI.preflight;
    const own=j.scene===ERA5_UI.scene&&(!j.product||j.product===ERA5_UI.task?.product),active=state.busy&&j.status==='running',context=era5ContextCurrent();
    $('#era5CredentialStatus').textContent=c.present?(c.provider==='cds'?'CDS':'GDEX')+' · реквизиты в памяти, доступ проверится запросом':'Доступ не задан. GDEX сначала проверяется публично.';
    $('#era5Runtime').textContent=(r.pyrttov?'Обёртка RTTOV загружается. ':'RTTOV 13.2 не подключён. ')+(r.coefficient.present?'Таблица коэффициентов на диске. ':'Таблица загрузится автоматически, без RTTOV. ')+(r.data_dependencies_missing.length?'Библиотеки установятся автоматически: '+r.data_dependencies_missing.join(', '):'Библиотеки ERA5 проверены.');
    let next=own&&!active&&j.next_action&&j.status!=='applied'?j.next_action:p?.next_action;
    if(own&&!active&&j.status==='data_ready'&&!j.data_only){next=!r.coefficient.present?'coefficients':!r.pyrttov?'engine':$('#era5Zenith').value===''?'geometry':'start';}
    if(!state.busy&&next!=='coefficients'&&p?.next_action==='archive')next='archive';
    if(!state.busy&&p?.next_action==='credentials'&&!['archive','coefficients'].includes(next))next='credentials';
    const message=!context?'Выбранный срок или продукт изменился. Результат относится к показанному сроку; откройте подготовку для нового выбора.':state.busy?(active?j.phase:state.operation):own&&j.status!=='idle'?j.phase:p?.message||'Проверяю готовность…';
    $('#era5Access').hidden=next!=='credentials'&&(c.present&&c.provider===$('#era5Provider').value||$('#era5Provider').value==='gdex');
    $('#era5EngineHelp').hidden=!(own&&!r.pyrttov&&['data_ready','coefficients_ready'].includes(j.status));
    $('#era5GeometryHelp').hidden=!(r.pyrttov||own&&j.next_action==='geometry');
    if(p?.orbit?.zenith_deg!=null && $('#era5Zenith').value===''){
      $('#era5Zenith').value=p.orbit.zenith_deg;
      $('#era5OrbitInfo').textContent=`Орбита SGP4 (${p.orbit.tle_source}): подспутниковая точка ${p.orbit.subpoint.lat}°N, ${p.orbit.subpoint.lon}°E, высота ${p.orbit.subpoint.alt_km} км. Зенитный угол: ${p.orbit.zenith_deg}°.`;
      if(next==='geometry')next='start';
    }
    const button=$('#era5Run');button.disabled=state.busy||!context;
    button.textContent=next==='archive'?'Выбрать архивный срок':next==='credentials'?'Подключить доступ':next==='coefficients'?'Загрузить коэффициенты':next==='engine'?'Подключить RTTOV':next==='geometry'?'Указать угол':next==='apply'?'Применить шкалу и открыть продукт':next==='queue'?'Открыть загрузки':next==='files'?'Открыть файлы':j.status==='data_ready'&&own?'Продолжить расчёт':j.status==='error'||j.status==='cancelled'||j.status==='interrupted'?'Повторить подготовку':'Подготовить и рассчитать';
    button.dataset.action=next||'start';
    $('#era5Cancel').hidden=!active;$('#era5Cancel').disabled=!active;
    for(const id of ['#era5Sync','#era5ClearCredential','#era5Setup','#era5ConnectEngine','#era5ChooseFile','#era5SaveToken'])$(id).disabled=state.busy;
    for(const id of ['#era5North','#era5South','#era5East','#era5West','#era5Provider','#era5Zenith','#era5TryRecent'])$(id).disabled=active;
    era5ShowSteps(state,p);
    if(own&&j.report_id&&j.status!=='running'&&ERA5_UI.report?.id!==j.report_id){
      const report=await api('/api/era5/report?'+qs({id:j.report_id}));
      if(generation===ERA5_UI.generation){ERA5_UI.report=report;era5DrawReport(report);}
    }
  }finally{ERA5_UI.refreshing=false;}
}
async function showEra5Dialog(required){
  if(!S.scene){left('catalog');$('#catalogCalendar').open=true;toast('Выберите срок. Затем подготовка найдёт каналы автоматически.');return;}
  cancelProductFlow();UI.wantedBuild=null;clearTimeout(UI.buildTimer);
  if(ERA5_UI.scene!==S.scene.id)$('#era5Zenith').value='';
  ERA5_UI.generation++;ERA5_UI.scene=S.scene.id;ERA5_UI.report=null;ERA5_UI.preflight=null;
  ERA5_UI.task={scene:S.scene.id,platform:S.scene.platform,time:S.scene.time,product:$('#product').value,channel:Number($('#channel').value)||9,preset:$('#preset').value};
  const spec=S.registry.products.find(p=>p.id===ERA5_UI.task.product);
  const needed=ERA5_UI.task.product==='cth'?[7,9,10]:ERA5_UI.task.product==='channel'?[ERA5_UI.task.channel]:spec?.channels||required||[];
  ERA5_UI.channels=needed.filter(c=>c>=4&&c<=10);if(!ERA5_UI.channels.length)throw Error('Выберите продукт с ИК-каналами 4–10. Для RGB температурная шкала не восстанавливается.');
  $('#era5Scene').textContent=(S.scene.platform==='ARCM1'?'Арктика-М1':'Арктика-М2')+' · '+S.scene.time.replace('T',' ').replace('Z',' UTC');
  $('#era5Report').replaceChildren();era5Error();$('#era5Run').disabled=true;$('#era5Next').textContent='Проверяю готовность…';
  if($('#calibrationDialog').open)$('#calibrationDialog').close();if(!era5Dialog.open)era5Dialog.showModal();
  const generation=ERA5_UI.generation;
  const state=await api('/api/era5/state');if(generation!==ERA5_UI.generation)return;
  if(state.credentials.provider)$('#era5Provider').value=state.credentials.provider;
  try{
    await era5Check();await era5Refresh();
    // Repair the previous release's data-ready pause once per opening, not on every poll.
    const current=ERA5_UI.state,j=current?.job;
    if(generation===ERA5_UI.generation&&era5Dialog.open&&era5ContextCurrent()&&!current?.busy&&
       j?.scene===ERA5_UI.scene&&j.status==='data_ready'&&!j.data_only&&!current.runtime.coefficient.present){
      await era5DownloadCoefficients();
    }
  }catch(e){era5Error(e.message);}
  clearInterval(ERA5_UI.timer);ERA5_UI.timer=setInterval(()=>{if(era5Dialog.open)era5Refresh().catch(e=>era5Error(e.message));},1200);
}
function era5DrawReport(report){
  let html='<h3>Результат</h3><p class="hint">'+escape(report.message||'')+'</p>';
  if(report.fallback_area)html+='<p class="hint" style="color:#1d6f42;font-weight:600;">✓ Опорная акватория: автоматически выбран чистый район «'+escape(report.fallback_area)+'».</p>';
  if(report.era5_files)html+='<p class="hint">Файлов ERA5 проверено: '+report.era5_files.length+'. Повторный запуск использует кэш.</p>';
  const passed=[];
  for(const [ch,row] of Object.entries(report.channels||{})){
    if(row.status==='passed'){const p=row.proposal;passed.push(Number(ch));html+=`<article class="reference-card"><h3>Канал ${escape(ch)} · проверен</h3><p>Tя = ${num(p.scale,6)} × DN + ${num(p.offset,3)} K</p><p>Проверено: ${p.valid_temperature_k.map(v=>num(v-273.15,1)).join('…')} °C · ${p.n} опор. За этим диапазоном значения скрываются.</p><p>Расхождение на группах: ${num(p.group_cv_rmse_k,2)} K. Не полная погрешность прибора.</p></article>`;}
    else html+=`<p class="hint">Канал ${escape(ch)}: ${escape(row.reason)}</p>`;
  }
  html+=`<a class="export-link" href="/api/era5/report?${qs({id:report.id})}" download="era5-calibration.json">Сохранить отчёт</a>`;
  if(passed.length)html+='<button id="era5Apply" class="primary full">Применить проверенные каналы</button>';
  $('#era5Report').innerHTML=html;
  if(passed.length)$('#era5Apply').onclick=()=>era5Apply().catch(e=>era5Error(e.message));
}
async function era5Apply(){
  if(!era5ContextCurrent())throw Error('Выбранный срок или продукт изменился. Эта шкала не будет применена к новому выбору.');
  const report=ERA5_UI.report;const channels=Object.entries(report?.channels||{}).filter(([ch,r])=>r.status==='passed').map(([ch])=>Number(ch));
  if(!channels.length)throw Error('Нет прошедших проверку каналов.');
  await api('/api/era5/apply',{id:report.id,scene:ERA5_UI.scene,channels,acknowledged:true});era5Dialog.close();requestBuild();
}
async function era5Start(only=false){
  era5Error();$('#era5Run').disabled=true;$('#era5Sync').disabled=true;try{if(!era5ContextCurrent())throw Error('Выбор изменился. Откройте подготовку для текущего срока.');await api('/api/era5/start',{...era5Input(),data_only:only});ERA5_UI.report=null;$('#era5Report').replaceChildren();await era5Refresh();}catch(e){era5Error(e.message);$('#era5Run').disabled=false;$('#era5Sync').disabled=false;}
}
async function era5DownloadCoefficients(){
  era5Error();$('#era5Run').disabled=true;
  try{await api('/api/era5/coefficients',{acknowledged:true});await era5Refresh();}
  catch(e){era5Error(e.message);$('#era5Run').disabled=false;}
}
$('#era5Run').onclick=async()=>{
  era5Error();try{
    const action=$('#era5Run').dataset.action;
    if(action==='archive'){
      const date=ERA5_UI.preflight?.suggested_date;era5Dialog.close();left('catalog');$('#catalogCalendar').open=true;if(date)setDate(date);
      toast('Ищу архивные снимки. Предложенная дата не означает подтверждённую публикацию ERA5.');
      if(date){await loadDay();const current=await api('/api/state');if(!current.busy&&!S.scenes.length&&S.day===date)await api('/api/search',{date,platform:ERA5_UI.task.platform,scope:'day'});}return;
    }
    if(action==='credentials'){$('#era5Access').hidden=false;$('#era5Token').focus();return;}
    if(action==='coefficients'){await era5DownloadCoefficients();return;}
    if(action==='engine'){$('#era5EngineHelp').hidden=false;$('#era5EnginePath').focus();return;}
    if(action==='geometry'){$('#era5GeometryHelp').hidden=false;$('#era5Zenith').focus();return;}
    if(action==='queue'){$('#queueOpen').click();return;}
    if(action==='files'){era5Dialog.close();await showFiles(ERA5_UI.task);return;}
    if(action==='apply'){await era5Apply();return;}
    await era5Start(false);
  }catch(e){era5Error(e.message);}
};
$('#era5Sync').onclick=()=>era5Start(true);
$('#era5ChooseFile').onclick=()=>$('#era5CredentialFile').click();
async function era5AccessSaved(r){$('#era5Provider').value=r.provider;await era5Check();await era5Refresh();if(ERA5_UI.preflight?.next_action==='start')await era5Start(false);}
$('#era5CredentialFile').onchange=async()=>{
  const sequence=++ERA5_UI.requestSerial;const file=$('#era5CredentialFile').files[0];$('#era5CredentialFile').value='';if(!file)return;era5Error();
  try{if(file.size>65536)throw Error('Файл доступа больше 64 КиБ.');const text=await file.text();if(sequence!==ERA5_UI.requestSerial)return;const r=await api('/api/era5/credentials',{text,format:'auto'});if(sequence===ERA5_UI.requestSerial&&era5Dialog.open)await era5AccessSaved(r);}catch(e){era5Error(e.message);}
};
$('#era5SaveToken').onclick=async()=>{era5Error();const text=$('#era5Token').value;$('#era5Token').value='';try{await era5AccessSaved(await api('/api/era5/credentials',{text,format:'token'}));}catch(e){era5Error(e.message);}};
$('#era5ClearCredential').onclick=async()=>{era5Error();try{++ERA5_UI.requestSerial;await api('/api/era5/credentials',{clear:true});await era5Check();await era5Refresh();}catch(e){era5Error(e.message);}};
$('#era5CheckPlan').onclick=async()=>{era5Error();try{await era5Check();await era5Refresh();}catch(e){era5Error(e.message);}};
async function era5UpdateOrbit(){
  const area=['#era5North','#era5West','#era5South','#era5East'].map(id=>$(id).value===''?null:Number($(id).value));
  if(area.includes(null)||!ERA5_UI.scene)return;
  try{
    const r=await api('/api/era5/orbit',{scene:ERA5_UI.scene,area});
    if(r.zenith_deg!=null){
      $('#era5Zenith').value=r.zenith_deg;
      $('#era5OrbitInfo').textContent=`Орбита SGP4 (${r.tle_source}): подспутниковая точка ${r.subpoint.lat}°N, ${r.subpoint.lon}°E, высота ${r.subpoint.alt_km} км. Зенитный угол: ${r.zenith_deg}°.`;
      await era5Check();await era5Refresh();
      if($('#era5Zenith').value!==''){$('#era5Run').dataset.action='start';$('#era5Run').textContent='Продолжить расчёт';}
    }
  }catch(_){}
}
$('#era5AreaPreset').onchange=async()=>{
  const val=$('#era5AreaPreset').value;
  if(val==='custom'||val==='auto')return;
  const [n,w,s,e]=val.split(',').map(Number);
  $('#era5North').value=n;$('#era5West').value=w;$('#era5South').value=s;$('#era5East').value=e;
  await era5UpdateOrbit();
};
for(const id of ['#era5Provider','#era5North','#era5South','#era5West','#era5East','#era5TryRecent','#era5Zenith'])$(id).onchange=async()=>{
  era5Error();try{
    if(['#era5North','#era5South','#era5West','#era5East'].includes(id)){
      $('#era5AreaPreset').value='custom';
      await era5UpdateOrbit();
    }
    await era5Check();
    if(ERA5_UI.state?.job.status==='data_ready'&&$('#era5Zenith').value!=='')ERA5_UI.state.job.next_action='start';
    await era5Refresh();
    if(id==='#era5Zenith'&&$('#era5Zenith').value!==''){$('#era5Run').dataset.action='start';$('#era5Run').textContent='Продолжить расчёт';}
  }catch(e){era5Error(e.message);}
};
$('#era5ConnectEngine').onclick=async()=>{era5Error();try{const r=await api('/api/era5/engine',{path:$('#era5EnginePath').value});$('#era5EngineMessage').textContent=r.message;await era5Check();await era5Refresh();if(r.pyrttov){$('#era5EngineHelp').hidden=true;$('#era5GeometryHelp').hidden=false;$('#era5Run').dataset.action=$('#era5Zenith').value!==''?'start':'geometry';$('#era5Run').textContent=$('#era5Zenith').value!==''?'Продолжить расчёт':'Указать угол';}}catch(e){era5Error(e.message);}};
$('#era5CalcZenith').onclick=async()=>{
  era5Error();$('#era5OrbitInfo').textContent='Запрашиваю актуальные TLE и рассчитываю положение спутника…';
  try{await era5UpdateOrbit();}catch(e){era5Error(e.message);$('#era5OrbitInfo').textContent='';}
};
$('#era5Setup').onclick=async()=>{era5Error();try{await api('/api/era5/setup',{});await era5Refresh();}catch(e){era5Error(e.message);}};
$('#era5Cancel').onclick=async()=>{try{await api('/api/era5/cancel',{id:ERA5_UI.state?.job.id});await era5Refresh();}catch(e){era5Error(e.message);}};
era5Dialog.addEventListener('close',()=>{clearInterval(ERA5_UI.timer);ERA5_UI.generation++;ERA5_UI.requestSerial++;$('#era5Token').value='';});
const manualScaleDialog=showScaleDialog;
showScaleDialog=async function(required){
  await manualScaleDialog(required);if(!$('#calibrationDialog').open)return;
  const body=$('#calibrationDialog .dialog-body'),section=document.createElement('section');section.innerHTML='<button id="era5Open" class="tonal full">Подготовить автоматически по ERA5</button><p class="micro">Установка библиотек, нужные каналы и запрос данных — одним запуском.</p>';body.prepend(section);
  $('#era5Open').onclick=()=>showEra5Dialog(required).catch(e=>scaleError(e.message));
};
