"""Изолируемая вычислительная часть ERA5. Без сервера, установки и смены UI-контекста."""
from __future__ import annotations
import datetime as dt
from pathlib import Path
from .download import atomic_json, digest
from .era5_access import retrieve_plan, cancelled
from .era5_forward import download_coefficients, forward_isolated
from .era5_sync import collocate, fit_reference, load_group
from .network import Cancelled


def calculate_reference(scene, plan, options, root, credential, identity, cancel=None, progress=lambda message: None):
    root=Path(root);folder=root/'era5'/'runs'/identity;folder.mkdir(parents=True,exist_ok=True)
    only=options.get('data_only') is True;zenith=options.get('zenith_deg')
    report={'id':identity,'scene':scene['id'],'platform':scene['platform'],'time':scene['time'],
            'plan':{k:v for k,v in plan.items() if k!='runtime'},'channels':{},'status':'running',
            'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'scope':'scene','scientific_validation':False}
    try:
        inputs=[] if only else [{'channel':c,'sha256':digest(scene['channels'][str(c)]['path'],cancel),
                                'filename':Path(scene['channels'][str(c)]['path']).name} for c in plan['channels']]
        files=retrieve_plan(plan,credential,root/'era5'/'cache',cancel,progress)
        report['era5_files']=[{k:v for k,v in f.items() if k!='path'} for f in files]
        report['inputs']=inputs
        progress('Проверяю ERA5: переменные, оба часа, сетка и единицы.')
        for group in ('pressure','surface'):
            checked=load_group(files,group,plan);checked.close()
        if not only or options.get('prepare_coefficients') is True:
            progress('Коэффициенты: проверяю таблицу Электро-Л №2; программа RTTOV для загрузки не нужна.')
            try:
                coefficient=download_coefficients(root,cancel,progress)
                if not coefficient.get('present'): raise ValueError('Таблица не прошла проверку.')
                report['coefficient']=coefficient
            except Cancelled:
                raise
            except Exception as exc:
                report['next_action']='coefficients'
                detail=str(exc) if isinstance(exc,ValueError) else 'Проверьте соединение с NWP SAF.'
                raise ValueError('Не удалось подготовить коэффициенты. ERA5 сохранена. '+detail) from None
        if only:
            report.update(status='data_ready',message='ERA5 загружена и проверена. Температурная шкала ещё не рассчитана.')
        else:
            progress('Отбираю опоры: открытая вода, оба часа ERA5, исходные пиксели.')
            pairs=collocate(scene,plan,files,zenith,cancel)
            report['selection']={k:v for k,v in pairs.items() if k not in ('profiles','dn','groups')}
            report['n_references']=len(pairs['profiles'])
            if len(pairs['profiles'])<24: raise ValueError('Недостаточно подходящих опор (нужно 24). Измените опорную область или срок; лёд и облака не заменяются эталонами.')
            progress('RTTOV: рассчитываю сигнал спектрального аналога Электро-Л №2.')
            bt=forward_isolated(pairs['profiles'],plan['channels'],root,folder,cancel)
            import numpy as np
            if bt.shape!=(len(pairs['profiles']),len(plan['channels'])) or not np.isfinite(bt).all():
                raise ValueError('Размерность выхода RTTOV не совпала с выбранными каналами.')
            raw=np.asarray(pairs['dn'],float)
            progress('Проверяю шкалу каждого канала на отложенных пространственных группах.')
            for j,ch in enumerate(plan['channels']):
                cancelled(cancel)
                try:
                    proposal=fit_reference(raw[:,j],bt[:,j],pairs['groups'])
                    proposal.update(source_sha256=inputs[j]['sha256'],calibration_run=identity,
                                    coefficient_sha256=coefficient['sha256'],srf_sha256=plan['srf_sha256'],
                                    geometry={'type':'constant_assumption','zenith_deg':zenith},applied_from=scene['time'])
                    report['channels'][str(ch)]={'status':'passed','proposal':proposal}
                except ValueError as exc:
                    report['channels'][str(ch)]={'status':'rejected','reason':str(exc)}
            report['coefficient']=coefficient
            report['assumptions']=['Спектральная чувствительность Электро-Л №2 вместо Арктики.',
                'ERA5 — модельные опоры; облачная маска не независима.',
                'Постоянный зенитный угол '+str(zenith)+'° во всей опорной области.',
                'Верхняя граница ERA5 1 гПа, интерполяция/верхний столб по RTTOV.',
                'CO₂ и прочие газы по стандартному профилю RTTOV; q2m по точке росы (Magnus).',
                'Линейная DN→BT проверена только внутри диапазона опор.']
            report['status']='ready' if any(c['status']=='passed' for c in report['channels'].values()) else 'rejected'
            report['message']='Шкала проверена; вне диапазона опор температуры не вычисляются.'
        for item in inputs:
            if digest(scene['channels'][str(item['channel'])]['path'],cancel)!=item['sha256']:
                raise ValueError('Исходные данные изменились во время расчёта. Шкала не применяется.')
        cancelled(cancel)
    except Cancelled:
        report.update(status='cancelled',message='Операция отменена; завершённые файлы сохранены.')
        raise
    except Exception as exc:
        # All ValueErrors raised above are local contract messages; never expose a provider exception.
        report.update(status='error',message=str(exc) if isinstance(exc,ValueError) else 'Сбой расчёта ERA5. Проверьте данные и RTTOV; повторный запуск использует кэш.')
    finally:
        atomic_json(folder/'report.json',report)
    return report
