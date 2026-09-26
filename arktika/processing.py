"""Численные и цветосинтезированные продукты. Сетевые URI не передаются GDAL."""
import hashlib,json,math,os,shutil
from pathlib import Path
import numpy as np
import rasterio
from rasterio.warp import reproject,Resampling
from rasterio.enums import ColorInterp
from PIL import Image
from .products import RECIPES
from .geo import grid,lonlat_grid,solar_elevation,map_context
from .download import digest,atomic_json
PALETTE=[[25,46,88],[40,92,164],[47,170,194],[216,236,222],[251,211,89],[235,110,63],[158,30,65]]
FLAGS=[dict(value=0,color='#00000000',name='Нет данных'),dict(value=1,color='#896ad8',name='Холодный ИК-сигнал: T9 < 233,15 K'),dict(value=2,color='#f2ba4b',name='Ночной спектральный признак низкой водяной облачности'),dict(value=3,color='#e55365',name='Спектральная несогласованность / контроль качества'),dict(value=4,color='#718497',name='Признак не выделен; безопасность не оценена')]
class CalibrationError(ValueError):pass

def validate_calibration(config):
 if not isinstance(config,dict):raise CalibrationError('Калибровка должна быть JSON-объектом.')
 if set(config)-({'mode','reference','channels','blocked'} if config.get('mode')=='custom' else {'mode','reference','channels'}):raise CalibrationError('Неизвестные поля калибровки.')
 mode=config.get('mode','unknown')
 if mode not in ('unknown','assumed','declared','custom'):raise CalibrationError('Неизвестный режим калибровки.')
 if mode=='custom':
  from .radiometry import affine_values
  if not isinstance(config.get('channels'),dict):raise CalibrationError('Нужен словарь шкал каналов.')
  if not isinstance(config.get('blocked',[]),list) or any(str(ch) not in {str(i) for i in range(4,11)} for ch in config.get('blocked',[])):raise CalibrationError('Неверные заблокированные шкалы.')
  for key,c in config['channels'].items():
   if str(key) not in {str(i) for i in range(4,11)} or not isinstance(c,dict) or c.get('units')!='K' or c.get('status') not in ('metadata','declared','assumed'):raise CalibrationError('Неверная шкала ИК-канала.')
   affine_values(c.get('scale'),c.get('offset'))
 if mode=='declared':
  if not isinstance(config.get('reference'),str) or not config['reference'].strip() or not isinstance(config.get('channels'),dict) or not config['channels']:raise CalibrationError('Нужны источник и коэффициенты калибровки.')
  for ch,c in config['channels'].items():
   try:
    valid=str(ch) in {str(i) for i in range(4,11)} and isinstance(c,dict) and c.get('units')=='K' and not isinstance(c.get('scale'),bool) and not isinstance(c.get('offset'),bool)
    scale,offset=float(c['scale']),float(c['offset'])
    valid=valid and math.isfinite(scale) and math.isfinite(offset) and scale>0
   except (KeyError,TypeError,ValueError):valid=False
   if not valid:raise CalibrationError('Яркостная температура: ИК-каналы 4–10, положительный scale, конечный offset, units=K. Видимые каналы 1–3 не переводятся в K.')
 return config

def calibration(ds,ch,config):
 validate_calibration(config);mode=config.get('mode','unknown')
 if not 1<=int(ch)<=10:raise CalibrationError('Номер канала: 1–10.')
 # Reflection channels need a separate radiance/reflectance calibration contract.
 if int(ch)<4:return 1.,0.,'DN','unknown','Видимый канал: температурная шкала неприменима'
 if mode=='assumed':return 1.,0.,'K','assumed','Допущение DN=K; поставщиком не подтверждено'
 if mode=='declared':
  c=config.get('channels',{}).get(str(ch))
  if not c:raise CalibrationError('Нет коэффициентов канала '+str(ch))
  return float(c['scale']),float(c['offset']),'K','declared',config['reference']
 if mode=='custom' and str(ch) in config.get('blocked',[]):return 1.,0.,'DN','unknown','Противоречивые метаданные: автоматическая шкала отключена'
 if mode=='custom' and str(ch) in config['channels']:
  c=config['channels'][str(ch)]
  return float(c['scale']),float(c['offset']),'K',c['status'],c.get('reference','')
 from .radiometry import metadata_scale
 try:c=metadata_scale(ds)
 except ValueError as exc:raise CalibrationError(str(exc)) from exc
 if c:return c['scale'],c['offset'],'K','metadata',c['reference']
 return 1.,0.,'DN','unknown','Единицы не объявлены'

def read_grid(path,ch,g,cal,kelvin_required=False):
 with rasterio.open(path) as ds:
  if not ds.crs or ds.count!=1:raise ValueError('Нужен одноканальный GeoTIFF с системой координат.')
  scale,offset,unit,status,reference=calibration(ds,ch,cal)
  if kelvin_required and unit!='K':raise CalibrationError('Для продукта нужна яркостная температура. Настройте температурную шкалу для необходимых каналов.')
  out=np.full((g['height'],g['width']),np.nan,dtype='float32')
  reproject(rasterio.band(ds,1),out,src_transform=ds.transform,src_crs=ds.crs,src_nodata=ds.nodata,dst_transform=g['transform'],dst_crs=g['crs'],dst_nodata=np.nan,resampling=Resampling.nearest,num_threads=1,warp_mem_limit=128)
  out=out*scale+offset
  return out,dict(channel=ch,units=unit,status=status,reference=reference,scale=scale,offset=offset,source_crs=ds.crs.to_string(),source_shape=[ds.height,ds.width])
def stretch(a,lo,hi,gamma=1):
 if not hi>lo or gamma<=0:raise ValueError('Неверная цветовая шкала.')
 return np.power(np.clip((a-lo)/(hi-lo),0,1),1/gamma)
def ramp(a,lo,hi):
 x=np.nan_to_num(stretch(a,lo,hi),nan=0)*(len(PALETTE)-1)
 return np.stack([np.interp(x,np.arange(len(PALETTE)),[c[i] for c in PALETTE]) for i in range(3)],axis=-1).astype('uint8')
def stats(a):
 v=a[np.isfinite(a)]
 if not v.size:return dict(min=None,max=None,p02=None,p98=None,count=0)
 return dict(min=float(v.min()),max=float(v.max()),p02=float(np.percentile(v,2)),p98=float(np.percentile(v,98)),count=int(v.size))

def build_product(scene,root,request,cal,cancel=None,progress=lambda x:None):
 product=request.get('product','channel');ch=int(request.get('channel',9));g=grid(request.get('preset','arctic'),request.get('width',1000))
 required=RECIPES[product]['channels'] if product in RECIPES else {'channel':[ch],'difference':[9,10],'indicators':[4,9,10],'phase':[7,9,10]}.get(product)
 if not required:raise ValueError('Этот продукт ещё не реализован; см. реестр.')
 missing=[c for c in required if str(c) not in scene['channels']]
 if missing:raise ValueError('Нет каналов: '+', '.join(map(str,missing)))
 arrays={};metas=[];inputs=[]
 for c in required:
  if cancel and cancel.is_set():
   from .network import Cancelled
   raise Cancelled()
  p=Path(scene['channels'][str(c)]['path']);progress('Перепроецирование канала '+str(c))
  before=p.stat();a,m=read_grid(p,c,g,cal,product!='channel');sha=digest(p,cancel);after=p.stat()
  if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('Исходный файл изменился во время чтения. Повторите расчёт.')
  arrays[c]=a;metas.append(m)
  inputs.append(dict(channel=c,filename=p.name,sha256=sha,size=after.st_size))
 common=np.logical_and.reduce([np.isfinite(a) for a in arrays.values()])
 if not common.any():raise ValueError('Нет общих валидных пикселей в выбранной области.')
 kelvin=all(m['units']=='K' for m in metas);qc=np.zeros(common.shape,dtype='uint8');qc[~common]=1
 invalid=common&np.logical_or.reduce([(a<120)|(a>400) for a in arrays.values()]) if kelvin else np.zeros(common.shape,bool);qc[invalid]|=2
 mismatch=common&(abs(arrays[10]-arrays[9])>15) if kelvin and 9 in arrays and 10 in arrays else np.zeros(common.shape,bool);qc[mismatch]|=4
 valid=common&~invalid;lon,lat=lonlat_grid(g);sun=solar_elevation(lon,lat,scene['time'])
 legend=dict(units=metas[0]['units'],calibration=metas,data_stats_scope='Конечная сетка выбранной области, не экстремумы всего исходного растра',nodata='Прозрачность = нет данных; не ясное небо',quality_flags={'1':'нет данных','2':'вне диагностического диапазона 120–400 K','4':'|T10−T9| > 15 K','8':'Солнце выше −6°; вне ночного режима'},threshold_status='Исследовательские диагностические пороги, не критерии подтверждённых ОЯ')
 from .interpretation import GUIDES, PHASE_CLASSES, THRESHOLDS, VERSION, phase_field
 legend['interpretation']=GUIDES.get(product,{})
 field=None
 if product=='channel':
  field=np.where(valid,arrays[ch],np.nan);ss=stats(field)
  lo,hi=(190.,300.) if kelvin else (ss['p02'],max(ss['p98'],ss['p02']+1))
  if request.get('display_min') is not None:lo=float(request['display_min'])
  if request.get('display_max') is not None:hi=float(request['display_max'])
  if not math.isfinite(lo+hi) or hi<=lo:raise ValueError('Проверьте границы цветовой шкалы.')
  rgb=ramp(field,lo,hi);title='Канал {} · {}'.format(ch,'яркостная температура' if kelvin else 'хранимые значения')
  legend.update(display_min=lo,display_max=hi,palette=PALETTE,stats=ss,meaning='Минимум/максимум — наблюдаемый диапазон сигнала. При K: более холодное/тёплое эффективное излучение; это не обязательно высота облаков. Цветовые пределы могут отличаться от экстремумов.')
 elif product in RECIPES:
  r=RECIPES[product];valid &= ~mismatch
  if product=='night':qc[common&(sun>-6)]|=8;valid &= sun<=-6
  parts=[];components=[]
  for name,(a,b,lo,hi,gamma) in zip('RGB',r['components']):
   value=arrays[a]-(arrays[b] if b else 0);parts.append(np.nan_to_num(stretch(value,lo,hi,gamma),nan=0))
   components.append(dict(component=name,formula='T'+str(a)+(' − T'+str(b) if b else ''),min=lo,max=hi,gamma=gamma,units='K',stats=stats(np.where(valid,value,np.nan))))
  rgb=(np.stack(parts,axis=-1)*255+.5).astype('uint8');title=r['title'];legend.update(units='RGB',components=components,meaning=r['meaning'],reference=r['reference'],formula='255 × clip((x−min)/(max−min),0,1)^(1/gamma)')
 elif product=='difference':
  field=np.where(valid,arrays[10]-arrays[9],np.nan);rgb=ramp(field,-4,2);title='Разность оконных каналов T10 − T9';legend.update(units='K',display_min=-4,display_max=2,palette=PALETTE,stats=stats(field),meaning='Положительное значение: канал 10 теплее канала 9. Это не влагозапас и не количество осадков.')
 elif product=='phase':
  field=phase_field(arrays,common).astype('float32');valid=common
  colours=np.array([[int(c['color'][k:k+2],16) for k in (1,3,5)] for c in PHASE_CLASSES],dtype='uint8')
  rgb=colours[field.astype(int)];title='Спектральные кандидаты фазы'
  legend.update(cloud_mask_available=False,validation_status='unvalidated_rules',units='class',classes=PHASE_CLASSES,thresholds=THRESHOLDS,meaning='Не карта ОЯ и не маска облачности. Приоритет: контроль качества, фазовый кандидат, холодный сигнал, неопределённость. Правила проверяются независимо от экранного RGB.')
 else:
  field=np.zeros(common.shape,dtype='float32');field[valid]=4;field[valid&(arrays[9]<233.15)]=1
  d=arrays[9]-arrays[4];field[valid&(sun<=-6)&(d>2)&(d<10)&(arrays[9]>243)]=2
  field[common&(mismatch|invalid)]=3;valid=common
  rgb=np.array([[0,0,0],[137,106,216],[242,186,75],[229,83,101],[113,132,151]],dtype='uint8')[field.astype(int)]
  title='Спектральные признаки';legend.update(units='class',classes=FLAGS,meaning='Не карта ОЯ и не маска облачности. Холодная поверхность тоже может иметь холодный сигнал. Приоритет: качество → ночной признак → холодный сигнал → прочее.')
 if field is not None:field=np.where(valid,field,np.nan).astype('float32')
 rgb[~valid]=0;rgba=np.dstack([rgb,np.where(valid,255,0).astype('uint8')])
 identity=hashlib.sha256(json.dumps(dict(inputs=inputs,request=request,cal=cal,grid={k:v for k,v in g.items() if k!='transform'},recipe=RECIPES.get(product),rules=THRESHOLDS,rules_version=VERSION,version='0.2.2'),sort_keys=True).encode()).hexdigest()[:24]
 final=Path(root)/identity
 if (final/'product.json').exists():return json.loads((final/'product.json').read_text(encoding='utf-8'))
 target=Path(root)/(identity+'.building')
 if target.exists():shutil.rmtree(target)
 target.mkdir(parents=True)
 profile=dict(driver='GTiff',width=g['width'],height=g['height'],crs=g['crs'],transform=g['transform'],compress='deflate',tiled=True,blockxsize=256,blockysize=256)
 status='assumed' if any(m['status']=='assumed' for m in metas) else ('unknown' if not kelvin else ('declared' if any(m['status']=='declared' for m in metas) else 'metadata'))
 tags=dict(product=product,scene=scene['id'],time=scene['time'],calibration_status=status,legend='legend.json')
 with rasterio.open(target/'display.tif','w',count=4,dtype='uint8',**profile) as ds:
  ds.write(rgba.transpose(2,0,1));ds.colorinterp=(ColorInterp.red,ColorInterp.green,ColorInterp.blue,ColorInterp.alpha);ds.update_tags(**tags)
 if field is not None:
  with rasterio.open(target/'values.tif','w',count=1,dtype='float32',nodata=np.nan,**profile) as ds:ds.write(field,1);ds.set_band_unit(1,legend['units']);ds.update_tags(**tags)
 with rasterio.open(target/'quality.tif','w',count=1,dtype='uint8',**profile) as ds:ds.write(qc,1);ds.update_tags(**tags)
 Image.fromarray(rgba).save(target/'map.png')
 night=np.zeros((*sun.shape,4),dtype='uint8');night[:,:,:3]=[13,26,50];night[:,:,3]=np.where(sun<0,90,0);Image.fromarray(night).save(target/'night.png')
 legend.update(title=title,scene_time=scene['time'],scene_id=scene['id'],product=product,status=status,valid_pixels=int(valid.sum()),total_pixels=int(valid.size),version='0.2.2')
 meta=dict(id=identity,scene_id=scene['id'],platform=scene['platform'],time=scene['time'],product=product,title=title,calibration_status=status,time_assumed=scene.get('time_assumed',True),grid={k:v for k,v in g.items() if k!='transform'},transform=list(g['transform'])[:6],inputs=inputs,request=request,calibration_config=cal,legend=legend,files=['map.png','night.png','display.tif','quality.tif','legend.json','product.json','report.html'])
 if field is not None:meta['files'].append('values.tif')
 atomic_json(target/'legend.json',legend)
 write_report(target,meta,g)
 atomic_json(target/'product.json',meta)
 os.replace(target,final)
 return meta

def write_report(target,meta,g):
 from html import escape
 import base64
 ctx=map_context(g,Path(__file__).resolve().parents[1]/'static/land.geojson')
 w,h=g['width'],g['height'];uri='data:image/png;base64,'+base64.b64encode((target/'map.png').read_bytes()).decode()
 polylines=''.join('<polyline fill="none" stroke="#657e8d" stroke-width="1" points="'+' '.join('{},{}'.format(*p) for p in line)+'"/>' for line in ctx['coast'])
 svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {} {}"><rect width="100%" height="100%" fill="#142d3d"/><image width="{}" height="{}" href="{}"/>{}</svg>'.format(w,h,w,h,uri,polylines)
 (target/'report.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><title>'+escape(meta['title'])+'</title><style>body{font:15px system-ui;max-width:1050px;margin:2em auto;color:#183444}svg{max-height:650px;width:100%}pre{white-space:pre-wrap;font-size:12px}p{line-height:1.6}</style><h1>'+escape(meta['title'])+'</h1><p>'+escape(meta['platform']+' · '+meta['time']+' · '+g['name'])+'</p><p>Калибровка: <b>'+escape(meta['calibration_status'])+'</b>. Цветовые признаки не подтверждают опасные явления. Отсутствие признака не означает безопасность.</p>'+svg+'<p>Natural Earth · обзорная подложка. UTC по правилу импорта; флаг предположения: '+str(meta['time_assumed'])+'</p>'+report_legend(meta['legend'])+'<details><summary>Численные параметры и происхождение</summary><pre>'+escape(json.dumps(meta['legend'],ensure_ascii=False,indent=2))+'</pre></details></html>',encoding='utf-8')


def report_legend(legend):
 from html import escape
 guide=legend.get('interpretation',{})
 out='<h2>'+escape(guide.get('title','Легенда'))+'</h2><p>'+escape(guide.get('purpose',''))+'</p>'
 for c in guide.get('swatches',[]):
  out+='<section><h3><span style="display:inline-block;width:1em;height:1em;border-radius:50%;margin-right:.5em;background:'+c['color']+'"></span>'+escape(c['title'])+'</h3><p>'+escape(c['meaning'])+'</p><p>'+escape(c['application'])+'</p></section>'
 if legend.get('palette'):
  colours=','.join('rgb('+','.join(map(str,c))+')' for c in legend['palette'])
  out+='<div style="height:16px;border-radius:8px;background:linear-gradient(to right,'+colours+')"></div><p>'+str(legend['display_min'])+' … '+str(legend['display_max'])+' '+escape(legend['units'])+'</p>'
 for c in legend.get('classes',[]):
  out+='<p><span style="display:inline-block;width:1em;height:1em;margin-right:.5em;background:'+c['color']+'"></span>'+escape(c['name'])+'</p>'
 return out
