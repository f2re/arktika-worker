"""Автокалибровка по модельным опорам: запрос → RTTOV → проверка → применение."""
from __future__ import annotations
import copy
import json
import re
import uuid
from .network import Cancelled
from .download import digest
from .era5_access import credentials_from_text, public_credentials, channels_list
from .era5_forward import download_coefficients, coefficient_info
from .era5_workflow import Era5WorkflowMixin
from .era5_runtime import runtime_info


class AutoCalibrationMixin(Era5WorkflowMixin):
    def _era5_init(self):
        with self.lock:
            if not hasattr(self,'_era5_credential'):
                self._era5_credential={};self._era5_job={'status':'idle'}
                try:
                    p=Path('~/.cdsapirc').expanduser()
                    if p.is_file():
                        self._era5_credential=credentials_from_text(p.read_text(encoding='utf-8'),'auto')
                        if self._era5_credential.get('provider')=='cds':
                            self._source_cds_credential=copy.deepcopy(self._era5_credential)
                except Exception:pass
                try:
                    saved=json.loads((self.store.root/'era5'/'workflow.json').read_text(encoding='utf-8'))
                    if saved.get('status')=='running':
                        saved.update(status='interrupted',phase='Сервер был остановлен. Готовые файлы сохранены; нажмите «Продолжить».',next_action='retry')
                        for step in saved.get('steps',[]):
                            if step.get('status')=='running':step['status']='waiting'
                    self._era5_job=saved
                except (OSError,ValueError):pass

    def era5_state(self):
        self._era5_init()
        runtime=runtime_info(self.store.root)
        with self.lock:
            return {'credentials':public_credentials(self._era5_credential),
                    'busy':self.busy,'operation':getattr(self,'status',''),
                    'runtime':{'data_dependencies_missing':runtime['missing'],'pyrttov':runtime['pyrttov'],
                        'coefficient':coefficient_info(self.store.root),'managed':runtime['managed']},
                    'job':copy.deepcopy(self._era5_job)}

    def era5_credentials(self,data):
        self._era5_init()
        with self.lock:
            if self.busy: raise ValueError('Дождитесь завершения операции перед изменением доступа ERA5.')
            parsed={} if data.get('clear') is True else credentials_from_text(data.get('text'),data.get('format','auto'))
            self._era5_credential=parsed
            # Unified CDS credentials also serve map fields; a clear really clears them.
            if not parsed or parsed.get('provider')=='cds':
                self._source_cds_credential=copy.deepcopy(parsed)
            return public_credentials(parsed)

    def era5_coefficient_download(self,data):
        if data.get('acknowledged') is not True:
            raise ValueError('Подтвердите загрузку официального архива RTTOV (лимит 1 ГиБ).')
        self._era5_init()
        with self.lock:
            if self.busy: raise ValueError('Подготовка уже выполняется; дождитесь завершения.')
            info=coefficient_info(self.store.root)
            if info['present']: return {'started':False,'coefficient':info}
            # Resume a stopped preparation without discarding its checked ERA5 report.
            saved=copy.deepcopy(self._era5_job)
            resume=saved.get('coefficient_resume') or {
                'status':saved.get('status','idle'), 'phase':saved.get('phase',''),
                'next_action':saved.get('next_action','start')}
            steps=saved.setdefault('steps',[])
            if not any(s['key']=='coefficients' for s in steps):
                pos=next((i for i,s in enumerate(steps) if s['key']=='engine'),len(steps))
                steps.insert(pos,{'key':'coefficients','title':'Коэффициенты Электро-Л №2','status':'pending'})
            saved.update(id=saved.get('id') or uuid.uuid4().hex,status='running',
                         next_action=None,coefficient_resume=resume)
            self._era5_job=saved
            self._flow_step('coefficients','running','Коэффициенты: загружаю таблицу; ERA5 повторно не запрашивается.')
            def work():
                try:
                    info=download_coefficients(self.store.root,self.cancel,
                        lambda m:self._flow_step('coefficients','running',m))
                    if not info.get('present'): raise ValueError('Таблица не прошла проверку.')
                    self._flow_step('coefficients','done','Таблица загружена и проверена. Программа RTTOV устанавливается отдельно.')
                    with self.lock:
                        status=resume['status'] if resume['status'] in ('data_ready','ready','applied','rejected') else 'coefficients_ready'
                        action=resume['next_action'] if status!='coefficients_ready' else 'start'
                        self._era5_job.update(status=status,next_action=action,
                            phase='Коэффициенты готовы. '+('Для расчёта подключите программу RTTOV 13.2.' if action=='engine' else 'Можно продолжить подготовку.'))
                        self._era5_job.pop('coefficient_resume',None);self._save_flow()
                    return {'era5_coefficients':info}
                except Exception as exc:
                    safe=exc if isinstance(exc,(Cancelled,ValueError)) else ValueError('Не удалось загрузить таблицу NWP SAF; проверьте соединение и повторите.')
                    message=self._flow_failed(safe)
                    with self.lock: self._era5_job['next_action']='coefficients';self._save_flow()
                    if isinstance(exc,Cancelled): raise
                    return {'era5_stopped':True,'message':message}
            self.start('Коэффициенты МСУ-ГС Электро-Л №2',work)
        return {'started':True,'id':saved['id']}

    def era5_start(self,data):
        return self.era5_workflow_start(data)

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
            self._era5_init()
            if self._era5_job.get('id')==report['id']:
                self._era5_job.update(status='applied',phase='Шкала применена к этому сроку. Исходные файлы не изменены.',next_action='complete')
                self._save_flow()
            return {'ok':True,'scene':scene['id'],'channels':selected,'scope':'scene','status':'assumed',
                    'message':'Вне диапазона опор значения скрываются; исходники не изменены.'}
