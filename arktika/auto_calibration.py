"""Автокалибровка по модельным опорам: запрос → RTTOV → проверка → применение."""
from __future__ import annotations
import copy
import json
import re
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
            return public_credentials(parsed)

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
