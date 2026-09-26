"""План продукции по одному сроку и сохранённые температурные шкалы."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import numpy as np
import rasterio
from .products import PRODUCTS
from .radiometry import affine_values, metadata_scale, two_points, fit_pairs


class ProductFlowMixin:
    def radiometry_config(self, scene):
        from .processing import calibration
        saved = self.store.setting('scale_profiles', {})
        entries = {**saved.get(scene['platform'], {}), **saved.get(scene['id'], {})}
        channels, issues = {}, {}
        for ch, entry in scene['channels'].items():
            if int(ch) < 4 or not Path(entry['path']).is_file():
                continue
            chosen = entries.get(ch)
            try:
                if chosen is not None:
                    if chosen.get('method') == 'auto':
                        chosen = None
                        legacy = {'mode':'unknown'}
                    else:
                        if chosen.get('source_sha256'):
                            from .download import digest
                            if digest(entry['path']) != chosen['source_sha256']:
                                raise ValueError('Исходник изменился: повторите опорную калибровку.')
                        channels[ch] = copy.deepcopy(chosen)
                        continue
                else:
                    legacy = self.cal
                with rasterio.open(entry['path']) as ds:
                    if legacy.get('mode') == 'assumed' or (legacy.get('mode') == 'declared' and ch in legacy.get('channels',{})):
                        a,b,u,s,r = calibration(ds,int(ch),legacy)
                        if u == 'K':
                            channels[ch] = dict(scale=a,offset=b,units=u,status=s,reference=r,method='legacy')
                    else:
                        match = metadata_scale(ds, entry.get('raster_bands'))
                        if match:
                            channels[ch] = match
            except (ValueError,OSError,rasterio.errors.RasterioError) as exc:
                issues[ch] = str(exc)
        return dict(mode='custom', reference='Шкалы каналов выбранного срока', channels=channels, blocked=list(issues)), issues

    def product_plan(self, data):
        product = str(data.get('product','channel'))
        spec = next((p for p in PRODUCTS if p['id']==product), None)
        if not spec or spec['status']=='planned':
            raise ValueError('Этот продукт не реализован.')
        platform, stamp = data.get('platform'), data.get('time')
        if not platform or not stamp:
            return dict(status='needs_time', product=product, message='Выберите срок наблюдения.')
        if platform not in ('ARCM1','ARCM2'):
            raise ValueError('Выберите аппарат.')
        from .interpretation import utc
        stamp = utc(stamp).isoformat(timespec='seconds').replace('+00:00','Z')
        session = self.session(platform,stamp)
        value = data.get('channel',9)
        if isinstance(value,bool) or str(value) not in {str(i) for i in range(1,11)}:
            raise ValueError('Канал: целое число 1–10.')
        channel = int(value)
        if not 1 <= channel <= 10:
            raise ValueError('Канал: 1–10.')
        required = [7,9,10] if product=='cth' else [channel] if product=='channel' else spec.get('channels',[])
        inventory = {c['channel']:c for c in session['channel_inventory']}
        missing = [ch for ch in required if inventory[ch]['state']!='local']
        blocked = [ch for ch in missing if inventory[ch]['state']=='unregistered']
        waiting = [ch for ch in missing if inventory[ch]['state'] in ('queued','running')]
        download = [inventory[ch]['candidate_id'] for ch in missing if inventory[ch]['candidate_id']]
        absent = [ch for ch in missing if inventory[ch]['state']=='absent']
        scene = self.scene(session['local_id']) if session['local_id'] else None
        config, issues = self.radiometry_config(scene) if scene else ({'channels':{}},{})
        unscaled = [ch for ch in required if ch not in missing and str(ch) not in config['channels']] if product not in ('channel','archive_rgb','motion','water') else []
        state = 'blocked' if blocked else 'catalog' if absent else 'download' if download else 'waiting' if waiting else 'calibration' if unscaled else 'ready'
        if product == 'archive_rgb':
            images=session['imagery']
            if data.get('composite'):
                images=[i for i in images if i.get('composite_id')==data['composite'] or i['id']==data['composite']]
            local=next((i for i in images if i['state']=='local'),None)
            active=next((i for i in images if i['state'] in ('queued','running')),None)
            candidate=next((i for i in images if i['state'] in ('remote','error','paused')),None)
            state='ready' if local else 'waiting' if active else 'download' if candidate else 'unavailable'
            download=[candidate['id']] if candidate and not (local or active) else []
        else:
            local=None
        sizes={a['id']:a.get('size') for a in session['assets']}
        return dict(product=product,status=state,scene_id=session['local_id'],platform=platform,time=stamp,
            required=required,missing=missing,blocked=blocked,waiting=waiting,absent=absent,
            download_ids=download,bytes=sum(sizes.get(i) or 0 for i in download),
            unknown_sizes=sum(sizes.get(i) is None for i in download),
            calibration_needed=unscaled,calibration_issues=issues,
            calibration={ch:{k:v for k,v in c.items() if k in ('status','reference','method')} for ch,c in config['channels'].items()},
            composite=local['composite_id'] if local else None,
            needs_profile=product in ('cth','water'),display_product='channel' if product=='cth' else product)

    def scale_inventory(self, data):
        self.sync_downloads()
        scene=self.scene(data['scene']); config,issues=self.radiometry_config(scene); rows=[]
        for ch,entry in sorted(scene['channels'].items(),key=lambda kv:int(kv[0])):
            if int(ch)<4 or not Path(entry['path']).is_file():
                continue
            with rasterio.open(entry['path']) as ds:
                a=ds.read(1,out_shape=(min(ds.height,256),min(ds.width,256)),masked=True)
                values=np.asarray(a.compressed(),float); values=values[np.isfinite(values)]
                sample=np.percentile(values,[0,2,25,50,75,98,100]).tolist() if values.size else []
                rows.append(dict(channel=int(ch),filename=entry['filename'],sample_dn=sample,
                    sample_scope='Выборка не более 256×256 пикселей, не точные экстремумы всей сцены',
                    scale=config['channels'].get(ch),issue=issues.get(ch,'')))
        return dict(scene=scene['id'],platform=scene['platform'],time=scene['time'],channels=rows)

    def scale_proposal(self, data):
        method=data.get('method','linear')
        if method=='two_anchors':
            result=two_points(data.get('dn1'),data.get('t1'),data.get('dn2'),data.get('t2'))
        elif method=='matched_pairs':
            result=fit_pairs(data.get('text'))
            result['pairs_sha256']=hashlib.sha256(data['text'].encode()).hexdigest()
        elif method in ('linear','declared'):
            a,b=affine_values(data.get('scale'),data.get('offset'))
            result=dict(scale=a,offset=b,method=method,validation='not_independently_validated')
        elif method=='auto':
            return dict(method='auto')
        else:
            raise ValueError('Неизвестный способ настройки шкалы.')
        reference=data.get('reference','')
        if not isinstance(reference,str):
            raise ValueError('Источник коэффициентов должен быть текстом.')
        result.update(units='K',status='declared' if method=='declared' else 'assumed',
                      reference=reference.strip()[:500])
        if method in ('declared','two_anchors','matched_pairs') and not result['reference']:
            raise ValueError('Укажите источник коэффициентов или опорных температур.')
        if not result['reference']:
            result['reference']='Исследовательская линейная шкала оператора; не калибровка поставщика'
        return result

    def save_scale(self, data):
        if self.busy:
            raise ValueError('Дождитесь завершения расчёта.')
        self.sync_downloads();scene=self.scene(data['scene'])
        channels=data.get('channels',[])
        if not isinstance(channels,list) or not channels or any(isinstance(c,bool) or str(c) not in scene['channels'] or not 4<=int(c)<=10 for c in channels):
            raise ValueError('Выберите скачанные ИК-каналы 4–10.')
        proposal=self.scale_proposal(data)
        if proposal.get('status')=='assumed' and data.get('acknowledged') is not True:
            raise ValueError('Подтвердите исследовательский статус шкалы.')
        scope=data.get('scope','scene')
        if scope not in ('scene','platform'):
            raise ValueError('Область шкалы: этот срок или этот аппарат.')
        with self.store.lock:
            profiles=self.store.setting('scale_profiles',{})
            key=scene['id'] if scope=='scene' else scene['platform']
            profile=profiles.setdefault(key,{})
            for ch in channels:
                profile[str(ch)]=dict(proposal,applied_from=scene['time'])
                if scope=='platform':
                    profiles.get(scene['id'],{}).pop(str(ch),None)
            self.store.set_setting('scale_profiles',profiles)
        return dict(ok=True,scene=scene['id'],scope=scope,channels=channels,proposal=proposal)

    def preview_scale_image(self, data):
        """Временный расчёт тем же обработчиком, без изменения шкал и продуктов."""
        import base64
        import tempfile
        from .processing import build_product
        from .products import RECIPES
        scene=self.scene(data['scene'])
        proposal=self.scale_proposal(data)
        selected=data.get('channels',[])
        if not selected or any(isinstance(c,bool) or str(c) not in scene['channels'] or not 4<=int(c)<=10 for c in selected):
            raise ValueError('Выберите скачанные ИК-каналы.')
        cal,_=self.radiometry_config(scene)
        for ch in selected:
            key=str(ch)
            cal['blocked']=[k for k in cal['blocked'] if k!=key]
            if proposal['method']=='auto':
                cal['channels'].pop(key,None)
                entry=scene['channels'][key]
                try:
                    with rasterio.open(entry['path']) as ds:
                        match=metadata_scale(ds,entry.get('raster_bands'))
                    if match:cal['channels'][key]=match
                except ValueError:
                    cal['blocked'].append(key)
            else:
                cal['channels'][key]=proposal
        product=data.get('product','channel')
        if product not in RECIPES and product not in ('channel','phase','difference','indicators'):
            product='channel'
        channel=int(data.get('channel',selected[0]))
        if str(channel) not in scene['channels']:channel=int(selected[0])
        request=dict(product=product,channel=channel,preset=data.get('preset','arctic'),width=256)
        with tempfile.TemporaryDirectory(prefix='arktika-scale-preview-') as folder:
            result=build_product(scene,folder,request,cal)
            image=(Path(folder)/result['id']/'map.png').read_bytes()
        return dict(image='data:image/png;base64,'+base64.b64encode(image).decode('ascii'),
                    title=result['title'],status=result['calibration_status'],persisted=False,
                    message='Предпросмотр. Шкалы и сохранённые продукты не изменены.')
