"""Автокалибровка по модельным опорам: запрос → RTTOV → проверка → применение."""
from __future__ import annotations
import copy
import datetime as dt
import json
import re
import uuid
from pathlib import Path
from .download import atomic_json, digest
from .era5_access import (credentials_from_text,public_credentials,plan_requests,channels_list,
                          retrieve_plan,cancelled)
from .era5_forward import readiness,download_coefficients,coefficient_info,spectral_passport,forward_isolated
from .era5_sync import collocate,fit_reference,load_group
from .network import Cancelled
from .radiometry import finite


class AutoCalibrationMixin:
    def _era5_init(self):
        with self.lock:
            if not hasattr(self,'_era5_credential'):
                self._era5_credential={};self._era5_job={'status':'idle'}

    def era5_state(self):
        self._era5_init()
        with self.lock:
            return {'credentials':public_credentials(self._era5_credential),
                    'busy':self.busy,'operation':getattr(self,'status',''),'runtime':readiness(self.store.root),'job':copy.deepcopy(self._era5_job)}

    def era5_credentials(self,data):
        self._era5_init()
        with self.lock:
            if self.busy: raise ValueError('Дождитесь завершения операции перед изменением доступа ERA5.')
            parsed={} if data.get('clear') is True else credentials_from_text(data.get('text'),data.get('format','auto'))
            self._era5_credential=parsed
            return public_credentials(parsed)

    def era5_plan(self,data):
        self.sync_downloads()
        if not data.get('scene'): raise ValueError('Выберите срок наблюдения; затем откройте автокалибровку.')
        scene=self.scene(data['scene']);channels=channels_list(data.get('channels',[]))
        missing=[c for c in channels if str(c) not in scene['channels'] or not Path(scene['channels'][str(c)]['path']).is_file()]
        if missing: raise ValueError('Сначала докачайте каналы выбранного продукта: '+', '.join(map(str,missing)))
        plan=plan_requests(scene['time'],channels,data.get('area',[80.,0.,60.,40.]),data.get('provider','cds'))
        plan.update(scene=scene['id'],platform=scene['platform'],
                    spectral_proxy='Электро-Л №2 / МСУ-ГС, не измеренная калибровка Арктики',
                    srf_sha256=spectral_passport()['sha256'],runtime=readiness(self.store.root))
        return plan

    def era5_coefficient_download(self,data):
        if data.get('acknowledged') is not True:
            raise ValueError('Подтвердите загрузку официального архива RTTOV (лимит 1 ГиБ).')
        self._era5_init()
        def work():
            info=download_coefficients(self.store.root,self.cancel,self.log)
            return {'era5_coefficients':info}
        self.start('Коэффициенты МСУ-ГС Электро-Л №2',work)
        return {'started':True}

    def era5_start(self,data):
        self._era5_init();plan=self.era5_plan(data)
        only=data.get('data_only') is True
        runtime=plan['runtime']
        if runtime['data_dependencies_missing']:
            raise ValueError('Установите зависимости ERA5: python -m pip install -r requirements-era5.txt')
        if not only and not runtime['pyrttov']:
            raise ValueError('Для расчёта опор нужен установленный RTTOV 13.2. Загрузить ERA5 можно отдельно.')
        if not only and data.get('acknowledged') is not True:
            raise ValueError('Подтвердите исследовательский статус спектрального аналога Электро-Л.')
        if not only:
            zenith=finite(data.get('zenith_deg'),'Зенитный угол наблюдения')
            if not 0<=zenith<=70: raise ValueError('Угол наблюдения: 0–70°. Не подставляйте угол без основания.')
            if data.get('constant_angle_acknowledged') is not True:
                raise ValueError('Подтвердите приближение постоянного угла для опорной области.')
            if not runtime['coefficient']['present'] and data.get('allow_coefficient_download') is not True:
                raise ValueError('Загрузите коэффициенты RTTOV или разрешите их получение.')
        else: zenith=None
        with self.lock:
            if self.busy: raise ValueError('Другая операция выполняется. Дождитесь завершения.')
            credential=copy.deepcopy(self._era5_credential)
            if plan['provider']=='cds' and credential.get('provider')!='cds':
                raise ValueError('Для CDS загрузите .cdsapirc или персональный токен CDS. Пароль NCAR здесь не подходит.')
            if credential and credential.get('provider')!=plan['provider']:
                raise ValueError('Выбранный источник не соответствует загруженному файлу доступа.')
            scene=copy.deepcopy(self.scene(plan['scene']))
            identity=uuid.uuid4().hex
            self._era5_job={'status':'running','id':identity,'scene':scene['id'],'phase':'Подготовка'}
            def progress(message):
                with self.lock: self._era5_job['phase']=message
                self.log(message)
            def work():
                report={'id':identity,'scene':scene['id'],'platform':scene['platform'],'time':scene['time'],
                        'plan':{k:v for k,v in plan.items() if k!='runtime'},'channels':{},'status':'running',
                        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                        'scope':'scene','scientific_validation':False}
                root=self.store.root/'era5';folder=root/'runs'/identity;folder.mkdir(parents=True)
                report_path=folder/'report.json'
                try:
                    inputs=[{'channel':c,'sha256':digest(scene['channels'][str(c)]['path'],self.cancel),
                             'filename':Path(scene['channels'][str(c)]['path']).name} for c in plan['channels']]
                    files=retrieve_plan(plan,credential,root/'cache',self.cancel,progress)
                    report['era5_files']=[{k:v for k,v in f.items() if k!='path'} for f in files]
                    report['inputs']=inputs
                    if only:
                        for group in ('pressure','surface'):
                            checked=load_group(files,group,plan)
                            checked.close()
                        report['status']='data_ready';report['message']='ERA5 сохранена в проверяемом кэше. Температурные коэффициенты ещё не рассчитаны.'
                    else:
                        coefficient=download_coefficients(self.store.root,self.cancel,progress)
                        progress('Отбираю опоры: открытая вода, оба часа ERA5, проверка исходных пикселей.')
                        pairs=collocate(scene,plan,files,zenith,self.cancel)
                        report['selection']={k:v for k,v in pairs.items() if k not in ('profiles','dn','groups')}
                        report['n_references']=len(pairs['profiles'])
                        if len(pairs['profiles'])<24: raise ValueError('Недостаточно подходящих опор (нужно 24). Лёд, облака и отсутствующие значения не заменяются эталонами.')
                        progress('RTTOV: рассчитываю сигнал спектрального аналога Электро-Л №2.')
                        bt=forward_isolated(pairs['profiles'],plan['channels'],self.store.root,folder,self.cancel)
                        import numpy as np
                        if bt.shape!=(len(pairs['profiles']),len(plan['channels'])) or not np.isfinite(bt).all():
                            raise ValueError('Размерность выхода RTTOV не совпала с выбранными каналами.')
                        raw=np.asarray(pairs['dn'],float)
                        for j,ch in enumerate(plan['channels']):
                            cancelled(self.cancel)
                            try:
                                proposal=fit_reference(raw[:,j],bt[:,j],pairs['groups'])
                                proposal.update(source_sha256=inputs[j]['sha256'],calibration_run=identity,
                                                coefficient_sha256=coefficient['sha256'],srf_sha256=plan['srf_sha256'],
                                                geometry={'type':'constant_assumption','zenith_deg':zenith},
                                                applied_from=scene['time'])
                                report['channels'][str(ch)]={'status':'passed','proposal':proposal}
                            except ValueError as exc:
                                report['channels'][str(ch)]={'status':'rejected','reason':str(exc)}
                        report['coefficient']=coefficient
                        report['assumptions']=['Спектральная чувствительность Электро-Л №2 вместо Арктики.',
                            'ERA5 — модельные опоры; облачная маска не независима.',
                            'Постоянный зенитный угол '+str(zenith)+'° во всей опорной области.',
                            'Верхняя граница ERA5 1 гПа, интерполяция/верхний столб по RTTOV.',
                            'CO₂ и прочие газы по стандартному профилю RTTOV; q2m по точке росы (Magnus).',
                            'Линейная эмпирическая зависимость DN→BT проверена только внутри диапазона опор.']
                        report['status']='ready' if any(c['status']=='passed' for c in report['channels'].values()) else 'rejected'
                        report['message']='Опоры рассчитаны. Применение — отдельно, только прошедшие проверку каналы, без экстраполяции.'
                    cancelled(self.cancel)
                    for item in inputs:
                        if digest(scene['channels'][str(item['channel'])]['path'],self.cancel)!=item['sha256']:
                            raise ValueError('Исходные спутниковые данные изменились во время расчёта. Шкала не применяется.')
                    atomic_json(report_path,report)
                    with self.lock: self._era5_job={'status':report['status'],'id':identity,'scene':scene['id'],'phase':report['message']}
                    return {'era5_report':identity,'status':report['status']}
                except Cancelled:
                    report.update(status='cancelled',message='Операция отменена; завершённые файлы ERA5 сохранены.')
                    atomic_json(report_path,report)
                    with self.lock: self._era5_job={'status':'cancelled','id':identity,'scene':scene['id'],'phase':report['message']}
                    raise
                except Exception as exc:
                    # Ошибки внешних библиотек/провайдера не раскрывают секреты или локальные пути.
                    message=str(exc) if isinstance(exc,ValueError) else 'Сбой автокалибровки. Проверьте данные и установленный RTTOV по справке.'
                    report.update(status='error',message=message);atomic_json(report_path,report)
                    with self.lock: self._era5_job={'status':'error','id':identity,'scene':scene['id'],'phase':message}
                    raise ValueError(message) from None
            self.start('ERA5: синхронизация' if only else 'ERA5: исследовательская автокалибровка',work)
        return {'started':True,'id':identity,'scene':scene['id']}

    def era5_report(self,identity):
        if not re.fullmatch('[0-9a-f]{32}',str(identity)): raise ValueError('Неверный идентификатор автокалибровки.')
        p=self.store.root/'era5'/'runs'/identity/'report.json'
        if not p.is_file(): raise ValueError('Отчёт ещё не готов.')
        return json.loads(p.read_text(encoding='utf-8'))

    def era5_apply(self,data):
        with self.lock:
            if self.busy: raise ValueError('Дождитесь окончания расчёта.')
            if data.get('acknowledged') is not True: raise ValueError('Подтвердите исследовательскую шкалу и ограниченный диапазон.')
            report=self.era5_report(data.get('id'))
            if report['status']!='ready': raise ValueError('Нет прошедшей проверку калибровки.')
            if report['scene']!=data.get('scene'): raise ValueError('Этот отчёт относится к другому сроку.')
            scene=self.scene(report['scene']);selected=channels_list(data.get('channels',[]));pending={}
            for ch in selected:
                item=report['channels'].get(str(ch),{})
                if item.get('status')!='passed': raise ValueError('Канал '+str(ch)+' не прошёл проверку.')
                proposal=item['proposal'];entry=scene['channels'].get(str(ch))
                if not entry or digest(entry['path'])!=proposal['source_sha256']:
                    raise ValueError('Исходный канал изменился после автокалибровки.')
                pending[str(ch)]=copy.deepcopy(proposal)
            with self.store.lock:
                saved=self.store.setting('scale_profiles',{});saved.setdefault(scene['id'],{}).update(pending)
                self.store.set_setting('scale_profiles',saved)
            return {'ok':True,'scene':scene['id'],'channels':selected,'scope':'scene','status':'assumed',
                    'message':'Вне диапазона опор значения скрываются; исходники не изменены.'}
