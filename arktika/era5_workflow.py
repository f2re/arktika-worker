"""Один запуск: библиотеки → нужные каналы → ERA5 → расчёт. Без фиктивной готовности."""
from __future__ import annotations
import copy
import datetime as dt
import json
from pathlib import Path
import time
import uuid
from .download import atomic_json
from .era5_access import channels_list, plan_requests, cancelled, public_credentials
from .era5_forward import coefficient_info, spectral_passport
from .era5_runtime import runtime_info, ensure_runtime, run_process, runtime_env, configure_engine
from .interpretation import utc
from .network import Cancelled
from .products import PRODUCTS

DEFAULT_AREA=[76.,10.,70.,20.]  # Норвежское море; не вся Арктика с одним углом.
STEPS=[('dependencies','Библиотеки'),('channels','Каналы снимка'),('era5','Данные ERA5'),('coefficients','Коэффициенты Электро-Л №2'),('engine','Программа RTTOV и геометрия'),('calibration','Шкала')]


class NeedAction(ValueError):
    def __init__(self,code,message):
        self.code=code;super().__init__(message)


def run_calculation(scene,plan,options,root,credential,identity,runtime,cancel,progress):
    folder=Path(root)/'era5'/'runs'/identity;folder.mkdir(parents=True,exist_ok=True)
    last=['']
    def tick():
        try: message=json.loads((folder/'progress.json').read_text(encoding='utf-8'))['message']
        except (OSError,ValueError,KeyError): return
        if message!=last[0]: last[0]=message;progress(message)
    payload=dict(scene=scene,plan=plan,options=options,root=str(root),credential=credential,identity=identity)
    script=Path(__file__).resolve().parents[1]/'scripts'/'era5_pipeline_worker.py'
    code=run_process([runtime['python'],script],cancel,timeout=10800,env=runtime_env(root),tick=tick,payload=payload)
    tick()
    if code or not (folder/'report.json').is_file(): raise ValueError('Расчётный процесс завершился без отчёта. Проверьте библиотеки и RTTOV; готовые файлы ERA5 сохранены.')
    return json.loads((folder/'report.json').read_text(encoding='utf-8'))


class Era5WorkflowMixin:
    def _era5_context(self,data):
        self.sync_downloads()
        if data.get('scene'):
            scene=self.scene(data['scene'])
        else:
            if data.get('platform') not in ('ARCM1','ARCM2') or not data.get('time'):
                raise ValueError('Выберите аппарат и срок наблюдения в «Снимках».')
            stamp=utc(data['time']);identity=data['platform']+'_'+stamp.strftime('%Y%m%d%H%M%S')
            scene={'id':identity,'platform':data['platform'],'time':stamp.isoformat(timespec='seconds').replace('+00:00','Z'),'channels':{}}
        product=data.get('product')
        if product:
            spec=next((p for p in PRODUCTS if p['id']==product),None)
            if not spec: raise ValueError('Неизвестный продукт.')
            required=[7,9,10] if product=='cth' else [data.get('channel',9)] if product=='channel' else spec.get('channels',[])
            channels=channels_list(required)
        else: channels=channels_list(data.get('channels',[]))
        return scene,channels

    def era5_plan(self,data):
        scene,channels=self._era5_context(data)
        plan=plan_requests(scene['time'],channels,data.get('area',DEFAULT_AREA),data.get('provider','cds'))
        plan.update(scene=scene['id'],platform=scene['platform'],srf_sha256=spectral_passport()['sha256'],
                    spectral_proxy='Электро-Л №2 / МСУ-ГС, исследовательский аналог')
        return plan

    def _channel_state(self,scene,channels):
        session=self.session(scene['platform'],scene['time'])
        inventory={i['channel']:i for i in session['channel_inventory']}
        rows=[inventory[c] for c in channels]
        return {'ready':[i['channel'] for i in rows if i['state']=='local'],
                'missing':[i['channel'] for i in rows if i['state']!='local'],'rows':rows}

    def era5_preflight(self,data):
        self._era5_init();scene,channels=self._era5_context(data);plan=self.era5_plan(data)
        inventory=self._channel_state(scene,channels);runtime=runtime_info(self.store.root)
        credential=public_credentials(self._era5_credential)
        access=plan['provider']=='gdex' and not credential['present'] or credential['provider']==plan['provider']
        if plan['provider']=='cds': access=credential['provider']=='cds'
        latest=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=6)).date().isoformat()
        recent=plan['likely_not_available'] and data.get('try_recent') is not True
        orbit=None
        try:
            from .orbit import calculate_viewing_geometry
            when=dt.datetime.fromisoformat(scene['time'].replace('Z','+00:00'))
            area=plan.get('area',[76.0,10.0,70.0,20.0])
            orbit=calculate_viewing_geometry(scene['platform'],when,(area[0]+area[2])/2.0,(area[1]+area[3])/2.0,self.store.root/'orbit')
        except Exception:pass
        return dict(plan=plan,inventory=inventory,credentials=credential,orbit=orbit,
                    runtime={'data_dependencies_missing':runtime['missing'],'pyrttov':runtime['pyrttov'],
                             'coefficient':coefficient_info(self.store.root),'managed':runtime['managed']},
                    next_action='archive' if recent else 'credentials' if not access else 'start',
                    suggested_date=latest,publication_estimated=True,
                    message='ERA5 этого срока пока не ожидается: обычная задержка около 5 суток. Выберите архивный срок; другой день не будет подставлен в расчёт.' if recent else
                            'Подключите доступ CDS один раз для этой сессии.' if not access else
                            'Готово к запуску. Недостающие библиотеки и каналы будут загружены автоматически.')

    def _save_flow(self):
        (self.store.root/'era5').mkdir(parents=True,exist_ok=True)
        atomic_json(self.store.root/'era5'/'workflow.json',self._era5_job)

    def _flow_step(self,key,status,message):
        with self.lock:
            self._era5_job['phase']=message;self._era5_job['active_step']=key
            for row in self._era5_job['steps']:
                if row['key']==key: row.update(status=status,message=message)
            self._save_flow()
        self.log(message)

    def era5_setup(self,data):
        self._era5_init()
        with self.lock:
            if self.busy: raise ValueError('Подготовка уже выполняется. Её ход показан сверху.')
            identity=uuid.uuid4().hex
            self._era5_job={'id':identity,'status':'running','phase':'Подготовка библиотек','steps':[{'key':k,'title':t,'status':'pending'} for k,t in STEPS]}
            self._save_flow()
            def work():
                try:
                    runtime=ensure_runtime(self.store.root,self.cancel,lambda m:self._flow_step('dependencies','running',m))
                    self._flow_step('dependencies','done','Библиотеки установлены и проверены.')
                    with self.lock: self._era5_job.update(status='setup_ready',next_action='start');self._save_flow()
                    return {'era5_setup':True}
                except Exception as exc:
                    self._flow_failed(exc);raise
            self.start('ERA5: установка библиотек',work)
        return {'started':True,'id':identity}

    def era5_engine(self,data):
        if self.busy: raise ValueError('Дождитесь завершения подготовки.')
        return configure_engine(self.store.root,data.get('path'))

    def era5_orbit(self,data):
        from .orbit import calculate_viewing_geometry
        scene_id=data.get('scene') or (self._era5_job.get('scene') if self._era5_job else None)
        if not scene_id: raise ValueError('Сцена не указана.')
        scene=self.scene(scene_id)
        if not scene: raise ValueError('Сцена не найдена.')
        when=dt.datetime.fromisoformat(scene['time'].replace('Z','+00:00'))
        area=data.get('area')
        if not area or len(area)!=4 or any(x is None for x in area):
            area=[76.0,10.0,70.0,20.0]
        orbit=calculate_viewing_geometry(scene['platform'],when,(area[0]+area[2])/2.0,(area[1]+area[3])/2.0,self.store.root/'orbit')
        return orbit

    def era5_cancel(self,data):
        self._era5_init()
        with self.lock:
            if self.busy and data.get('id')==self._era5_job.get('id') and self._era5_job.get('status')=='running':
                self.cancel.set();return {'cancelled':True}
        return {'cancelled':False}

    def _flow_failed(self,exc):
        is_cancel=isinstance(exc,Cancelled)
        message='Подготовка остановлена. Готовые файлы сохранены; можно продолжить.' if is_cancel else str(exc) if isinstance(exc,ValueError) else 'Сбой подготовки. Проверьте подключение и повторите; готовые файлы сохранены.'
        with self.lock:
            self._era5_job.update(status='cancelled' if is_cancel else 'needs_action' if isinstance(exc,NeedAction) else 'error',
                                 phase=message,next_action=getattr(exc,'code','retry'))
            for row in self._era5_job.get('steps',[]):
                if row['status']=='running': row.update(status='cancelled' if is_cancel else 'error',message=message)
            self._save_flow()
        return message

    def _ensure_channels(self,scene,channels,owned):
        searched=False;attempted=set();until=time.monotonic()+7200
        while True:
            cancelled(self.cancel)
            state=self._channel_state(scene,channels)
            if not state['missing']:
                identity=self.session(scene['platform'],scene['time'])['local_id']
                return copy.deepcopy(self.scene(identity))
            rows=state['rows']
            if any(r['state']=='unregistered' for r in rows):
                raise NeedAction('files','Скачанный канал не распознан. Откройте «Каналы и файлы»: там указана причина.')
            if any(r['state']=='absent' for r in rows):
                if searched: raise NeedAction('archive','В каталоге выбранного срока нет всех нужных каналов. Выберите другой срок.')
                searched=True
                self._flow_step('channels','running','Ищу каналы '+', '.join(map(str,state['missing']))+' в каталоге выбранного аппарата и срока.')
                begin=utc(scene['time']).replace(hour=0,minute=0,second=0,microsecond=0)
                self.client.stac(scene['platform'],begin.isoformat().replace('+00:00','Z'),
                    (begin+dt.timedelta(days=1)).isoformat().replace('+00:00','Z'),self.store.upsert,self.log,self.cancel)
                continue
            ids=[r['candidate_id'] for r in rows if r['state'] in ('remote','error','paused') and r.get('candidate_id')]
            if any(i in attempted for i in ids): raise NeedAction('queue','Загрузка канала остановилась. Откройте «Загрузки», проверьте доступ и нажмите «Продолжить».')
            if ids:
                before={j['id']:j['state'] for j in self.store.jobs()}
                self.queue_add(ids);owned.update(i for i in ids if before.get(i) not in ('queued','running'));attempted.update(ids)
            active=[j for j in self.store.jobs() if j['id'] in attempted]
            done=sum(j['done'] for j in active);total=sum(j['total'] or 0 for j in active)
            with self.lock: self._era5_job['channel_progress']={'ready':len(state['ready']),'total_channels':len(channels),'bytes':done,'total_bytes':total}
            self._flow_step('channels','running',f"Каналы: {len(state['ready'])}/{len(channels)} готовы."+(f' Загружено {done//1048576}/{total//1048576} МиБ.' if total else ' Загрузка продолжается.'))
            if time.monotonic()>until: raise NeedAction('queue','Истекло ожидание каналов. Готовые файлы сохранены; проверьте очередь.')
            self.cancel.wait(.8)

    def era5_workflow_start(self,data):
        self._era5_init();scene,channels=self._era5_context(data);plan=self.era5_plan(data)
        options=copy.deepcopy(data);only=data.get('data_only') is True
        # Values with no physical basis do not receive an invented nadir default.
        zenith=options.get('zenith_deg')
        if zenith is not None:
            from .radiometry import finite
            zenith=finite(zenith,'Зенитный угол')
            if not 0<=zenith<=70: raise ValueError('Зенитный угол: 0–70°.')
        geometry=zenith is not None and options.get('constant_angle_acknowledged') is True
        if not only and data.get('acknowledged') is not True:
            raise ValueError('Запуск требует принятия исследовательского статуса; шкала не является калибровкой поставщика.')
        options['zenith_deg']=zenith
        identity=uuid.uuid4().hex
        with self.lock:
            if self.busy: raise ValueError('Другая операция уже выполняется. Дождитесь завершения.')
            credential=copy.deepcopy(self._era5_credential)
            self._era5_job={'id':identity,'status':'running','scene':scene['id'],'time':scene['time'],'platform':scene['platform'],
                'channels':channels,'product':data.get('product'),'data_only':only,'started_at':dt.datetime.now(dt.timezone.utc).isoformat(),'phase':'Проверка срока и доступа','next_action':None,
                'steps':[{'key':k,'title':t,'status':'pending'} for k,t in STEPS]}
            self._save_flow()
            def work():
                owned=set()
                try:
                    if plan['likely_not_available'] and not data.get('try_recent'):
                        raise NeedAction('archive','ERA5 выбранного срока пока не ожидается (обычно задержка 5 суток). Выберите архивный срок; снимок не будет сопоставлен с другим днём.')
                    if plan['provider']=='cds' and credential.get('provider')!='cds' or credential and credential.get('provider')!=plan['provider']:
                        raise NeedAction('credentials','Подключите доступ выбранного источника. Файл .cdsapirc или токен CDS вводится один раз за сессию.')
                    self._flow_step('dependencies','running','Проверяю библиотеки ERA5.')
                    runtime=ensure_runtime(self.store.root,self.cancel,lambda m:self._flow_step('dependencies','running',m))
                    self._flow_step('dependencies','done','Библиотеки проверены; перезапуск не требуется.')
                    snapshot=scene
                    if only:
                        self._flow_step('channels','skipped','Для отдельной загрузки ERA5 спутниковые каналы не нужны.')
                    else:
                        snapshot=self._ensure_channels(scene,channels,owned)
                        self._flow_step('channels','done','Все '+str(len(channels))+' канала(ов) прочитаны и готовы.')
                    can_compute=not only and runtime['pyrttov'] and geometry
                    options['data_only']=not can_compute
                    # Preparing tables does not depend on the executable or geometry.
                    options['prepare_coefficients']=not only
                    if only: self._flow_step('coefficients','skipped','Для отдельной загрузки ERA5 таблица не требуется.')
                    self._flow_step('era5','running','Запрашиваю ERA5; каждый файл будет проверен по сроку, переменным и сетке.')
                    def progress(message):
                        key='coefficients' if message.startswith('Коэффициенты:') else 'engine' if message.startswith('RTTOV') else 'calibration' if message.startswith(('Отбираю','Проверяю шкалу')) else 'era5'
                        if key!='era5': self._flow_step('era5','done','ERA5 загружена и проверена.')
                        self._flow_step(key,'running',message)
                    report=run_calculation(snapshot,plan,options,self.store.root,credential,identity,runtime,self.cancel,progress)
                    with self.lock: self._era5_job['report_id']=identity;self._save_flow()
                    if report['status']=='error':
                        message=report['message']
                        if report.get('next_action')=='coefficients': raise NeedAction('coefficients',message)
                        if 'Доступ ERA5 отклонён' in message: raise NeedAction('credentials',message+' Примите условия обоих наборов или замените токен в форме ниже.')
                        if 'ERA5 не найден' in message: raise NeedAction('archive',message+' Выберите более ранний срок.')
                        raise ValueError(message)
                    self._flow_step('era5','done',f"ERA5: {len(report.get('era5_files',[]))}/{len(plan['requests'])} файлов проверены.")
                    if not only:
                        coefficient=report.get('coefficient',{})
                        if not coefficient.get('present'):
                            raise NeedAction('coefficients','Таблица коэффициентов не подтверждена. Повторите её загрузку; ERA5 сохранена.')
                        self._flow_step('coefficients','done','Таблица загружена и проверена. Повторная загрузка не требуется.')
                    if report['status']=='data_ready':
                        next_action='complete' if only else 'engine' if not runtime['pyrttov'] else 'geometry'
                        message='ERA5 загружена и проверена. Расчёт шкалы не запрошен.' if only else 'ERA5 и коэффициенты готовы. Не установлена программа RTTOV 13.2 — подключите её один раз.' if not runtime['pyrttov'] else 'Данные готовы. Укажите подтверждённый угол наблюдения; из DN он не определяется.'
                        self._flow_step('engine','skipped' if only else 'waiting',message)
                    else:
                        self._flow_step('engine','done','Расчёт опор RTTOV выполнен.')
                        passed=sum(r['status']=='passed' for r in report['channels'].values())
                        message=f'Проверено {passed}/{len(channels)} каналов. '+('Можно применить шкалу.' if passed==len(channels) else 'Полный продукт не готов; причины приведены ниже.')
                        self._flow_step('calibration','done' if passed==len(channels) else 'waiting' if passed else 'error',message)
                        next_action='apply' if passed else 'area'
                    cancelled(self.cancel)
                    with self.lock:
                        self._era5_job.update(status=report['status'],phase=message,next_action=next_action,report_id=identity)
                        self._save_flow()
                    return {'era5_report':identity,'status':report['status']}
                except Exception as exc:
                    if isinstance(exc,Cancelled):
                        for i in owned: self.queue.pause(i)
                    message=self._flow_failed(exc)
                    if isinstance(exc,Cancelled): raise
                    return {'era5_stopped':True,'message':message}
            self.start('ERA5: автоматическая подготовка',work)
        return {'started':True,'id':identity,'scene':scene['id']}
