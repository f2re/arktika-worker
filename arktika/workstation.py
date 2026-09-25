"""Модульный однопользовательский ГИС-сервис с каталогом и загрузчиком GPTL."""
import csv,datetime as dt,hashlib,io,json,re,threading,zipfile
from pathlib import Path
import numpy as np
import rasterio
from pyproj import Transformer
from affine import Affine
from .service import App,dto
from .auth import AuthClient
from .products import registry
from .processing import build_product,validate_calibration,calibration
from .geo import grid,map_context,project_points,solar_elevation
from .routes import route_profile
from .download import atomic_json,digest
from .model import UTC,iso,now
ROOT=Path(__file__).resolve().parents[1]
FILE_PATTERN=re.compile(r'^A([12])_(\d{14})_ch(\d{2})\.tif$',re.I)
from .analysis_service import AnalysisMixin
from .interpretation import spectral, profile_diagnostics
from .catalog import catalog_sessions, describe_session
from .archive import register_composite, build_composite
class Workstation(AnalysisMixin, App):
 def __init__(self,state_dir,download_dir=None,token='',config=None,client=None):
  self.config=config or {};super().__init__(state_dir,download_dir,token,client or AuthClient(token,self.config.get('oauth')))
  with self.store.lock,self.store.conn:
   self.store.conn.execute('CREATE TABLE IF NOT EXISTS scenes(id TEXT PRIMARY KEY,stamp TEXT,data TEXT)')
  self.product_root=self.store.root/'products';self.product_root.mkdir(exist_ok=True)
  self.route_root=self.store.root/'routes';self.route_root.mkdir(exist_ok=True)
  self.context_cache={};self.done_jobs={};self.registration_errors={}
  self.cal=self.store.setting('calibration',self.config.get('calibration',{'mode':'unknown'}));validate_calibration(self.cal)
 def state(self):
  self.sync_downloads()
  result=super().state();result.update(view_settings=self.store.setting('view_settings',{}),version='0.2.2',calibration=self.cal,oauth=dict(client_id=self.client.oauth.get('client_id',''),redirect_uri=self.client.oauth.get('redirect_uri',''),refresh_present=bool(self.client.refresh_token)))
  return result
 def configure(self,data):
  if 'view_settings' in data:
   from .products import PRESETS,PRODUCTS
   view=data['view_settings']
   if not isinstance(view,dict) or set(view)-{'preset','product','channel'}:raise ValueError('Неверные параметры вида.')
   allowed={p['id'] for p in PRODUCTS if p['status'] in ('implemented','experimental') and p['id']!='motion'}
   if view.get('preset') not in PRESETS or view.get('product') not in allowed or not 1<=int(view.get('channel',0))<=10:raise ValueError('Неизвестный продукт, область или канал.')
   self.store.set_setting('view_settings',dict(preset=view['preset'],product=view['product'],channel=int(view['channel'])))
  return super().configure(data)
 def set_calibration(self,value):
  if self.busy:raise ValueError('Дождитесь окончания расчёта перед сменой калибровки.')
  if not isinstance(value,dict):raise ValueError('Ожидается JSON-объект калибровки.')
  validate_calibration(value);self.cal=json.loads(json.dumps(value));self.store.set_setting('calibration',self.cal)
  return self.cal
 def register_file(self,path,asset=None):
  path=Path(path).expanduser().resolve();m=FILE_PATTERN.fullmatch(path.name)
  if not m:return register_composite(self,path,asset)
  platform='ARCM'+m[1];stamp=dt.datetime.strptime(m[2],'%Y%m%d%H%M%S').replace(tzinfo=UTC);ch=int(m[3])
  if not 1<=ch<=10:return None
  with rasterio.open(path) as ds:
   if ds.count!=1 or not ds.crs:return None
   entry=dict(path=str(path),filename=path.name,channel=ch,crs=ds.crs.to_string(),size=path.stat().st_size,shape=[ds.height,ds.width],nodata=float(ds.nodata) if ds.nodata is not None and np.isfinite(ds.nodata) else None)
  identity=platform+'_'+m[2]
  with self.store.lock,self.store.conn:
   old=self.store.conn.execute('SELECT data FROM scenes WHERE id=?',(identity,)).fetchone()
   scene=json.loads(old[0]) if old else dict(id=identity,platform=platform,time=iso(stamp),time_assumed=True,channels={})
   previous=scene['channels'].get(str(ch))
   if previous and previous['path']!=str(path) and previous['crs']=='EPSG:4326' and entry['crs']!='EPSG:4326':return identity
   if previous and previous['path']!=str(path) and previous['crs']==entry['crs']:
    # Different copies are not silently mixed: prefer the already registered path.
    if Path(previous['path']).is_file():return identity
   scene['channels'][str(ch)]=entry
   self.store.conn.execute('INSERT OR REPLACE INTO scenes VALUES(?,?,?)',(identity,scene['time'],json.dumps(scene,ensure_ascii=False)))
   self.store.revision+=1
  return identity
 def scan_local(self,path):
  p=Path(path).expanduser().resolve()
  if not p.is_dir():raise ValueError('Укажите существующую папку GeoTIFF.')
  found=0;seen=0;skipped=[]
  known={str(Path(j['path']).resolve()):self.store.asset(j['asset_id']) for j in self.store.jobs()}
  for f in p.rglob('*'):
   if self.cancel.is_set():break
   seen+=1
   if seen>100000:raise ValueError('Просмотрено 100 000 путей. Выберите более узкую папку.')
   if not f.is_file() or f.suffix.lower() not in ('.tif','.tiff'):continue
   try:
    if self.register_file(f,known.get(str(f.resolve()))):found+=1
    else:skipped.append(f.name)
   except (ValueError,OSError,rasterio.errors.RasterioError):skipped.append(f.name)
  self.log('Импорт GeoTIFF: {} файлов; не зарегистрировано: {}. Файлы не перемещены.'.format(found,len(skipped)))
  return dict(files=found,skipped=skipped)
 def sync_downloads(self):
  for job in self.store.jobs():
   if job['state']!='done':continue
   try:
    path=Path(job['path']);stat=path.stat();signature=(str(path),stat.st_size,stat.st_mtime_ns)
    if self.done_jobs.get(job['id'])==signature:continue
    asset=self.store.asset(job['asset_id'])
    if asset and asset.get('category') in ('channel','rgb'):
     try:
      if not self.register_file(path,asset):raise ValueError('Не удалось определить аппарат и срок файла.')
      self.registration_errors.pop(job['id'],None)
     except (ValueError,rasterio.errors.RasterioError):
      self.registration_errors[job['id']]='GeoTIFF не распознан для карты: проверьте цветовые полосы, геопривязку и метаданные срока.'
    self.done_jobs[job['id']]=signature
   except OSError:
    self.done_jobs.pop(job['id'],None)
 def jobs(self):
  rows=super().jobs();scenes={(s['platform'],s['time']):s for s in self.scenes()}
  for row in rows:
   local=scenes.get((row['platform'],row['time']),{})
   composite=next((c for c in local.get('composites',[]) if c.get('asset_id')==row['asset_id']),None)
   row['scene_id']=local.get('id')
   row['composite_id']=composite['id'] if composite else None
   asset=self.store.asset(row['asset_id']) or {}
   row['channel']=asset.get('channel')
   row['can_open']=row['state']=='done' and Path(row['path']).is_file() and bool(composite or (row['category']=='channel' and any(c['channel']==row['channel'] for c in local.get('channels',[]))))
   row['map_error']=self.registration_errors.get(row['id'],'')
  return rows
 def scenes(self,date='',platform=''):
  self.sync_downloads()
  with self.store.lock:rows=[json.loads(r[0]) for r in self.store.conn.execute('SELECT data FROM scenes WHERE stamp LIKE ? ORDER BY stamp, id',(date+'%',))]
  for scene in rows:
   scene['channels']={ch:c for ch,c in scene['channels'].items() if Path(c['path']).is_file()}
   scene['composites']={key:c for key,c in scene.get('composites',{}).items() if Path(c['path']).is_file() and c.get('asset_id') not in self.registration_errors}
  rows=[s for s in rows if s['channels'] or s['composites']]
  return [dict(id=s['id'],platform=s['platform'],time=s['time'],time_assumed=s.get('time_assumed',True),composites=[{k:v for k,v in c.items() if k!='path'} for c in sorted(s['composites'].values(),key=lambda c:(c['crs']!='EPSG:4326',c['size'],c['id']))],channels=[{k:v for k,v in c.items() if k!='path'} for c in sorted(s['channels'].values(),key=lambda x:x['channel'])]) for s in rows if not platform or s['platform']==platform]
 def scene(self,identity):
  with self.store.lock:r=self.store.conn.execute('SELECT data FROM scenes WHERE id=?',(identity,)).fetchone()
  if not r:raise ValueError('Сначала скачайте или импортируйте этот сеанс.')
  return json.loads(r[0])
 def calendar(self,month,filters):
  result=super().calendar(month,filters);scenes=self.scenes(month,filters['platform']);counts={}
  for s in scenes:counts[s['time'][:10]]=counts.get(s['time'][:10],0)+1
  for day in result['days']:day['local_scenes']=counts.get(day['date'],0)
  return result
 def sessions(self,day,filters,offset=0,limit=24):
  return catalog_sessions(self,day,filters,offset,limit)
 def session(self,platform,stamp):
  result=super().session(platform,stamp);records=[r for r in self.store.records(stamp,platform) if r['time']==stamp]
  local=next((s for s in self.scenes(stamp,platform) if s['time']==stamp),None)
  jobs={j['id']:j for j in self.store.jobs()}
  result.update(describe_session(result['assets'],local,jobs))
  result['local_files']=(local or {}).get('channels',[])
  result['local_composites']=(local or {}).get('composites',[])
  for asset in result['assets']:
   job=jobs.get(asset['id'])
   if asset['id'] in self.registration_errors:asset['map_error']=self.registration_errors[asset['id']]
   asset['composite_id']=next((c['id'] for c in result['local_composites'] if c.get('asset_id')==asset['id']),None)
   if job:asset['job_state']='missing' if job['state']=='done' and not Path(job['path']).is_file() else job['state']
  result['records']=[{k:r.get(k) for k in ('id','time','time_original','time_assumed','bbox','geometry','level','gsd')} for r in records]
  return result
 def prepare(self,data):
  scene=self.scene(data['scene']);request={k:data[k] for k in ('product','channel','preset','width','display_min','display_max','composite') if k in data};cal=json.loads(json.dumps(self.cal))
  def work():
   result=build_composite(scene,self.product_root,request,self.cancel,self.log) if request.get('product')=='archive_rgb' else build_product(scene,self.product_root,request,cal,self.cancel,self.log);self.log('Продукт готов: '+result['title']);return result
  self.start('Создание продукта',work)
 def products(self):
  rows=[]
  for p in self.product_root.glob('*/product.json'):
   if not re.fullmatch(r'[0-9a-f]{24}',p.parent.name):continue
   try:rows.append(json.loads(p.read_text(encoding='utf-8')))
   except (ValueError,OSError):continue
  return sorted(rows,key=lambda x:(x['time'],x['id']),reverse=True)
 def product(self,identity):
  if not re.fullmatch(r'[0-9a-f]{24}',identity):raise ValueError('Неверный идентификатор продукта.')
  p=self.product_root/identity/'product.json'
  if not p.exists():raise ValueError('Продукт не найден.')
  return json.loads(p.read_text(encoding='utf-8'))
 def artifact(self,identity,name):
  p=self.product(identity)
  if name not in p['files']:raise ValueError('Этот файл не относится к продукту.')
  return self.product_root/identity/name
 def export(self,identity):
  p=self.product(identity);out=io.BytesIO()
  with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
   for name in p['files']:z.write(self.artifact(identity,name),name)
  return out.getvalue()
 def product_grid(self,p):
  return dict(p['grid'],transform=Affine(*p['transform']))
 def context(self,preset='arctic',width=1000,product=None):
  g=self.product_grid(self.product(product)) if product else grid(preset,width);key=product or (preset,width)
  if key not in self.context_cache:self.context_cache[key]=map_context(g,ROOT/'static/land.geojson')
  return dict(grid={k:v for k,v in g.items() if k!='transform'},**self.context_cache[key])
 def coordinates(self,data):
  g=self.product_grid(self.product(data['product'])) if data.get('product') else grid(data.get('preset','arctic'),data.get('width',1000));x=float(data['x']);y=float(data['y'])
  if not np.isfinite(x+y) or not 0<=x<g['width'] or not 0<=y<g['height']:raise ValueError('Точка вне карты.')
  lon,lat=Transformer.from_crs(g['crs'],4326,always_xy=True).transform(*(g['transform']*(x,y)))
  if not np.isfinite(lon+lat) or not -180<=lon<=180 or not -90<=lat<=90:raise ValueError('Обратная проекция не определена в этой точке.')
  return dict(lon=lon,lat=lat)
 def frozen_scene(self,product):
  if product.get('display_only'):raise ValueError('Готовая RGB-композиция содержит цвета, а не исходные каналы. Для численного анализа откройте поканальный GeoTIFF.')
  scene=self.scene(product['scene_id'])
  for item in product['inputs']:
   entry=scene['channels'].get(str(item['channel']))
   if not entry or digest(entry['path'])!=item['sha256']:raise ValueError('Исходники изменились после построения продукта. Пересоздайте продукт перед анализом точки или маршрута.')
  return scene
 def pixel(self,data):
  p=self.product(data['product']);g=self.product_grid(p);xy=self.coordinates(dict(x=data['x'],y=data['y'],product=p['id']))
  scene=self.frozen_scene(p);values=[]
  for ch,entry in sorted(scene['channels'].items(),key=lambda x:int(x[0])):
   with rasterio.open(entry['path']) as ds:
    x,y=Transformer.from_crs(4326,ds.crs,always_xy=True).transform(xy['lon'],xy['lat']);inside=ds.bounds.left<=x<ds.bounds.right and ds.bounds.bottom<y<=ds.bounds.top
    val=next(ds.sample([(x,y)],indexes=1,masked=True))[0];ok=inside and not np.ma.is_masked(val) and np.isfinite(float(val))
    try:s,o,u,status,ref=calibration(ds,int(ch),p['calibration_config'])
    except ValueError:s,o,u,status,ref=1,0,'DN','unknown','Нет калибровки этого канала'
    values.append(dict(channel=int(ch),value=float(val)*s+o if ok else None,unit=u,calibration=status))
  ix=min(g['width']-1,int(float(data['x'])));iy=min(g['height']-1,int(float(data['y'])))
  with rasterio.open(self.artifact(p['id'],'quality.tif')) as ds:quality=int(ds.read(1,window=((iy,iy+1),(ix,ix+1)))[0,0])
  with rasterio.open(self.artifact(p['id'],'display.tif')) as ds:display_rgba=ds.read(window=((iy,iy+1),(ix,ix+1)))[:,0,0].tolist()
  value=None
  if 'values.tif' in p['files']:
   with rasterio.open(self.artifact(p['id'],'values.tif')) as ds:v=float(ds.read(1,window=((iy,iy+1),(ix,ix+1)))[0,0]);value=v if np.isfinite(v) else None
  return dict(**xy,channels=values,display_rgba=display_rgba,quality_flags=quality,product_value=value,product_unit=p['legend']['units'],sun_elevation=float(solar_elevation(xy['lon'],xy['lat'],p['time'])),time=p['time'],note='Каналы: исходные пиксели. Продукт: итоговая сетка карты; значения могут отличаться из-за перепроецирования.')
 def route(self,data):
  p=self.product(data['product']);scene=self.frozen_scene(p)
  inputs_before=self.sample_provenance(scene)
  result=route_profile(scene,data['points'],p['calibration_config'],data.get('step_km',25),data.get('speed_kmh',300),data.get('departure',''))
  profile=self.get_profile(data['profile_id']) if data.get('profile_id') else None
  for row in result['samples']:
   pixel=dict(lon=row['lon'],lat=row['lat'],time=row['observation_time'],
      sun_elevation=float(solar_elevation(row['lon'],row['lat'],row['observation_time'])),
      channels=[dict(channel=int(ch),value=v,unit=result['units'][ch]['unit'],calibration=result['units'][ch]['calibration']) for ch,v in row['channels'].items()])
   spectral_result=spectral(pixel)
   row['phase']=spectral_result['phase'];row['phase_label']=spectral_result['phase_label']
   row['metrics']={m['id']:m['value'] for m in spectral_result['metrics']}
   if profile:
    profile_pixel=dict(pixel,time=row['eta'] or row['observation_time'])
    diag=profile_diagnostics(profile,profile_pixel,spectral_result,data.get('altitude_m'))
    row['profile_result']=diag
  for feature,row in zip(result['geojson']['features'],result['samples']):
   feature['properties'].update({k:row[k] for k in ('phase','phase_label','metrics','profile_result') if k in row})
  result['profile_id']=profile['id'] if profile else None
  result['inputs_all']=self.sample_provenance(scene)
  if result['inputs_all']!=inputs_before:raise ValueError('Исходники изменились во время расчёта маршрута. Повторите расчёт.')
  from .interpretation import VERSION,THRESHOLDS
  result['method_version']=VERSION;result['thresholds']=dict(THRESHOLDS)
  result['profile_snapshot']=profile
  result['request']=dict(data)
  g=self.product_grid(p);result['map_points']=project_points(g,[(r['lon'],r['lat']) for r in result['samples']])
  identity=hashlib.sha256(json.dumps(dict(request=data,inputs=inputs_before,profile=profile,method=result['method_version']),sort_keys=True,allow_nan=False).encode()).hexdigest()[:24];result['id']=identity;result['product_id']=p['id'];result['inputs']=p['inputs'];result['calibration']=p['calibration_status'];atomic_json(self.route_root/(identity+'.json'),result)
  return result
 def route_export(self,identity,kind):
  if not re.fullmatch(r'[0-9a-f]{24}',identity):raise ValueError('Неверный идентификатор маршрута.')
  data=json.loads((self.route_root/(identity+'.json')).read_text(encoding='utf-8'))
  if kind=='geojson':return json.dumps(data['geojson'],ensure_ascii=False,allow_nan=False).encode('utf-8')
  if kind=='json':return json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8')
  if kind!='csv':raise ValueError('Формат: csv, geojson, json.')
  out=io.StringIO();channels=sorted(data['units'],key=int);w=csv.writer(out)
  w.writerow(['lon','lat','distance_km','eta_utc','observation_utc','age_minutes','status']+['ch'+ch+'_'+data['units'][ch]['unit'] for ch in channels]+['phase_hypothesis','d97_K','d109_K','profile_temperature_C','profile_liquid_water_g_m3','profile_layer_status'])
  for r in data['samples']:w.writerow([r['lon'],r['lat'],r['distance_km'],r['eta'],r['observation_time'],r['observation_age_minutes'],r['status']]+[r['channels'].get(ch) for ch in channels]+[r.get('phase'),r.get('metrics',{}).get('d97'),r.get('metrics',{}).get('d109'),r.get('profile_result',{}).get('layer',{}).get('temperature_c'),r.get('profile_result',{}).get('layer',{}).get('liquid_water_g_m3'),r.get('profile_result',{}).get('layer',{}).get('status')])
  return out.getvalue().encode('utf-8-sig')
 def motion(self,data):
  from .motion import analyse
  first=self.scene(data['first']);second=self.scene(data['second']);cal=json.loads(json.dumps(self.cal))
  def work():
   result=analyse(first,second,cal,data.get('preset','arctic'),int(data.get('channel',9)),self.cancel);atomic_json(self.store.root/'motion.json',result);self.log('Слежение завершено. Кандидаты вращения не являются подтверждёнными ПМЦ.');return result
  self.start('Слежение по двум срокам',work)
