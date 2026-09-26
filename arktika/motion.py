"""Исследовательское слежение по изображениям, не прогноз и не ветер на 10 м."""
import datetime as dt
import numpy as np
from pyproj import Transformer
from .geo import grid,GEOD
from .processing import read_grid

def block_match(a, b, patch=16, radius=5, stride=24, min_score=.65, cancel=None):
 """NCC with forward/backward consistency. Coordinates describe image motion only."""
 if a.shape != b.shape or a.ndim != 2:
  raise ValueError('Требуются две одинаковые двумерные сетки.')
 if patch < 4 or patch % 2 or radius < 1 or stride < 1:
  raise ValueError('Размер окна должен быть чётным; радиус и шаг — положительными.')
 half = patch // 2
 height, width = a.shape
 def match(source, target, x, y):
  if cancel and cancel.is_set():
   from .network import Cancelled
   raise Cancelled()
  template = source[y-half:y+half, x-half:x+half]
  if template.shape != (patch, patch) or not np.isfinite(template).all() or template.std() < .2:
   return None
  z = (template-template.mean()).ravel()
  zn = np.linalg.norm(z)
  scores = []
  for dy in range(-radius, radius+1):
   for dx in range(-radius, radius+1):
    tx, ty = x+dx, y+dy
    if tx-half < 0 or ty-half < 0 or tx+half > width or ty+half > height:
     continue
    sample = target[ty-half:ty+half, tx-half:tx+half]
    if not np.isfinite(sample).all() or sample.std() < .2:
     continue
    q = (sample-sample.mean()).ravel()
    scores.append((float(np.dot(z, q)/(zn*np.linalg.norm(q))), dx, dy))
  if not scores:
   return None
  scores.sort(reverse=True)
  score, dx, dy = scores[0]
  runner = max((r[0] for r in scores if abs(r[1]-dx)>1 or abs(r[2]-dy)>1), default=-1)
  if score < min_score or abs(dx)==radius or abs(dy)==radius or score-runner < .03:
   return None
  return score, dx, dy, score-runner
 rows = []
 for y in range(half+radius, height-half-radius, stride):
  for x in range(half+radius, width-half-radius, stride):
   forward = match(a, b, x, y)
   if forward is None:
    continue
   score, dx, dy, margin = forward
   backward = match(b, a, x+dx, y+dy)
   if backward is None:
    continue
   error = float(np.hypot(dx+backward[1], dy+backward[2]))
   if error > 1:
    continue
   rows.append(dict(x=x, y=y, dx=dx, dy=dy, correlation=score, peak_margin=margin,
                    forward_backward_error_px=error, backward_correlation=backward[0]))
 return rows

def rotations(vectors,seconds,radius=75):
 """Локальный линейный fit в координатах изображения; не атмосферная вихревость."""
 if len(vectors)<6:return []
 x=np.array([[r['x'],-r['y']] for r in vectors],float);u=np.array([[r['dx']/seconds,-r['dy']/seconds] for r in vectors],float);candidates=[]
 for i,center in enumerate(x):
  take=np.linalg.norm(x-center,axis=1)<radius
  if take.sum()<6:continue
  local=x[take]-center;design=np.column_stack((local,np.ones(take.sum())))
  if np.linalg.matrix_rank(design)<3:continue
  fit=np.linalg.lstsq(design,u[take],rcond=None)[0];prediction=design@fit
  variance=np.sum((u[take]-u[take].mean(axis=0))**2)
  if variance<1e-12:continue
  score=1-float(np.sum((u[take]-prediction)**2)/variance)
  a,b=fit[0,0],fit[1,0];d,e=fit[0,1],fit[1,1]
  omega=.5*(d-b);strain=.5*np.hypot(a-e,b+d)
  if omega>max(1e-5,strain) and score>.5:
   if any(np.hypot(c['x']-center[0],c['y']+center[1])<radius for c in candidates):continue
   candidates.append(dict(x=float(center[0]),y=float(-center[1]),angular_rate_s=float(omega),fit_score=score,status='candidate_rotation_not_pmc'))
 return candidates

def analyse(first,second,cal,preset='arctic',channel=9,cancel=None,cal_second=None):
 if first['platform']!=second['platform']:raise ValueError('Для MVP нужны два срока одного аппарата.')
 seconds=(dt.datetime.fromisoformat(second['time'].replace('Z','+00:00'))-dt.datetime.fromisoformat(first['time'].replace('Z','+00:00'))).total_seconds()
 if not 300<=seconds<=7200:raise ValueError('Интервал между сроками: 5–120 минут; второй срок должен быть позже.')
 if preset=='geographic':raise ValueError('Для движения выберите полярную область, а не сетку в градусах.')
 from .download import digest
 paths=[first['channels'][str(channel)]['path'],second['channels'][str(channel)]['path']]
 hashes=[digest(path,cancel) for path in paths]
 g=grid(preset,512);a,ma=read_grid(first['channels'][str(channel)]['path'],channel,g,cal);b,mb=read_grid(second['channels'][str(channel)]['path'],channel,g,cal_second if cal_second is not None else cal)
 if (ma['units'],ma['scale'],ma['offset'])!=(mb['units'],mb['scale'],mb['offset']):raise ValueError('Шкалы двух сроков различаются.')
 if cancel and cancel.is_set():
  from .network import Cancelled
  raise Cancelled()
 vectors=block_match(a,b,cancel=cancel)
 if hashes!=[digest(path,cancel) for path in paths]:raise ValueError('Исходники изменились во время слежения.')
 tr=Transformer.from_crs(g['crs'],4326,always_xy=True);at=g['transform']
 features=[]
 for v in vectors:
  lon,lat=tr.transform(*(at*(v['x']+.5,v['y']+.5)));lon2,lat2=tr.transform(*(at*(v['x']+.5+v['dx'],v['y']+.5+v['dy'])))
  az,_,dist=GEOD.inv(lon,lat,lon2,lat2);v.update(lon=lon,lat=lat,speed_m_s=dist/seconds,direction_to_deg=az%360)
  features.append(dict(type='Feature',geometry=dict(type='LineString',coordinates=[[lon,lat],[lon2,lat2]]),properties=dict(v)))
 candidates=rotations(vectors,seconds)
 for c in candidates:c['lon'],c['lat']=tr.transform(*(at*(c['x']+.5,c['y']+.5)))
 return dict(input_sha256=hashes,tracking_quality='NCC + forward/backward <= 1 pixel',first=first['id'],second=second['id'],seconds=seconds,grid={k:v for k,v in g.items() if k!='transform'},vectors=vectors,candidates=candidates,geojson=dict(type='FeatureCollection',features=features),status='experimental',note='Корреляционное слежение без надёжной оценки высоты и облачной маски. Берег/лёд могут давать неподвижный рисунок. Кандидат вращения не является обнаруженным ПМЦ. Прогноз возникновения ПМЦ не выполняется.')
