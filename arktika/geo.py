"""Общая геометрия растра, карты и маршрутов: longitude, latitude; UTC."""
import datetime as dt
import json
from pathlib import Path
import numpy as np
from pyproj import Transformer,Geod
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds
from .products import PRESETS
GEOD=Geod(ellps='WGS84')
def grid(preset='arctic',width=1000):
 if preset not in PRESETS:raise ValueError('Неизвестная область карты.')
 width=int(width)
 if not 256<=width<=2048:raise ValueError('Размер карты: 256–2048 пикселей.')
 p=PRESETS[preset];b=p.get('bounds') or transform_bounds(4326,p['crs'],*p['geo_bounds'],densify_pts=80)
 height=max(128,round(width*(b[3]-b[1])/(b[2]-b[0])))
 if height>2048:raise ValueError('Слишком высокая сетка.')
 return dict(preset=preset,name=p['name'],crs=p['crs'],bounds=list(b),width=width,height=height,transform=from_bounds(*b,width,height))
def lonlat_grid(g):
 a=g['transform'];x=a.c+(np.arange(g['width'])+.5)*a.a;y=a.f+(np.arange(g['height'])+.5)*a.e
 return Transformer.from_crs(g['crs'],4326,always_xy=True).transform(*np.meshgrid(x,y))
def solar_elevation(lon,lat,stamp):
 """Приближение NOAA; геометрическая высота без рефракции и рельефа."""
 t=dt.datetime.fromisoformat(stamp.replace('Z','+00:00'))
 if t.tzinfo is None:raise ValueError('У времени должен быть часовой пояс.')
 t=t.astimezone(dt.timezone.utc);h=t.hour+t.minute/60+t.second/3600
 year_days=(dt.date(t.year+1,1,1)-dt.date(t.year,1,1)).days
 f=2*np.pi/year_days*(t.timetuple().tm_yday-1+(h-12)/24)
 eq=229.18*(.000075+.001868*np.cos(f)-.032077*np.sin(f)-.014615*np.cos(2*f)-.040849*np.sin(2*f))
 d=.006918-.399912*np.cos(f)+.070257*np.sin(f)-.006758*np.cos(2*f)+.000907*np.sin(2*f)-.002697*np.cos(3*f)+.00148*np.sin(3*f)
 ha=np.deg2rad((h*60+eq+4*np.asarray(lon))/4-180);lat=np.deg2rad(lat)
 return np.rad2deg(np.arcsin(np.clip(np.sin(lat)*np.sin(d)+np.cos(lat)*np.cos(d)*np.cos(ha),-1,1)))
def project_points(g,points):
 tr=Transformer.from_crs(4326,g['crs'],always_xy=True);a=g['transform'];out=[]
 for lon,lat in points:
  x,y=tr.transform(lon,lat);out.append([(x-a.c)/a.a,(y-a.f)/a.e])
 return out

def map_context(g,land_path):
 obj=json.loads(Path(land_path).read_text())['geometry'];polys=obj['coordinates'] if obj['type']=='MultiPolygon' else [obj['coordinates']]
 # Project coordinate arrays in one pass; split at map discontinuities.
 tr=Transformer.from_crs(4326,g['crs'],always_xy=True);a=g['transform']
 def fast_split(coords):
  coords=np.asarray(coords);x,y=tr.transform(coords[:,0],coords[:,1]);xy=np.column_stack(((x-a.c)/a.a,(y-a.f)/a.e));chunks=[];chunk=[];prev=None
  for c,p in zip(coords,xy):
   if c[1]<20 or not np.isfinite(p).all():
    if len(chunk)>1:chunks.append(chunk)
    chunk=[];prev=None;continue
   if prev is not None and (abs(p[0]-prev[0])>g['width']*.55 or abs(p[1]-prev[1])>g['height']*.75):
    if len(chunk)>1:chunks.append(chunk)
    chunk=[]
   chunk.append([round(float(p[0]),2),round(float(p[1]),2)]);prev=p
  if len(chunk)>1:chunks.append(chunk)
  return chunks
 coast=[c for p in polys for ring in p for c in fast_split(ring)]
 grat=[]
 for lon in range(-180,180,30):grat.extend(fast_split([(lon,float(lat)) for lat in np.linspace(40,89.9,100)]))
 for lat in range(50,90,10):grat.extend(fast_split([(float(lon),lat) for lon in np.linspace(-180,180,361)]))
 labels=[]
 places=[('Гренландия',-42,74),('Северный полюс',0,90),('Баренцево море',37,74),('Карское море',78,76),('Море Лаптевых',124,76),('Аляска',-150,66),('Канада',-100,70),('Шпицберген',16,79),('Исландия',-19,65),('Мурманск',33.08,68.97)]
 for name,lon,lat in places:
  x,y=project_points(g,[(lon,lat)])[0]
  if 0<x<g['width'] and 0<y<g['height']:labels.append(dict(text=name,x=x,y=y))
 for lat in (60,70,80):
  x,y=project_points(g,[(-45,lat)])[0]
  if 0<x<g['width'] and 0<y<g['height']:labels.append(dict(text=str(lat)+'° с. ш.',x=x,y=y))
 return dict(coast=coast,graticule=grat,labels=labels,attribution='Natural Earth · обзорная подложка, не навигационная карта')
def densify_route(points,step_km=25,speed_kmh=300,departure=''):
 if not isinstance(points,list) or not 2<=len(points)<=200:raise ValueError('Нужны 2–200 точек маршрута.')
 p=np.asarray(points,float)
 if p.shape!=(len(points),2) or not np.isfinite(p).all() or (abs(p[:,0])>180).any() or (abs(p[:,1])>90).any():raise ValueError('Координаты: долгота, широта в градусах.')
 step_km=float(step_km);speed_kmh=float(speed_kmh)
 if not 1<=step_km<=500 or not .1<=speed_kmh<=2000:raise ValueError('Шаг: 1–500 км; скорость: 0,1–2000 км/ч.')
 depart=dt.datetime.fromisoformat(departure.replace('Z','+00:00')) if departure else None
 if depart and depart.tzinfo is None:raise ValueError('Время отправления должно содержать часовой пояс.')
 rows=[];distance=0.
 for start,end in zip(p[:-1],p[1:]):
  az,_,length=GEOD.inv(*start,*end);n=max(1,int(np.ceil(length/(step_km*1000))))
  if len(rows)+n+1>20000:raise ValueError('Слишком много точек маршрута.')
  for j in range(n):
   lon,lat,_=GEOD.fwd(*start,az,length*j/n);rows.append(dict(lon=lon,lat=lat,distance_km=(distance+length*j/n)/1000))
  distance+=length
 rows.append(dict(lon=float(p[-1,0]),lat=float(p[-1,1]),distance_km=distance/1000))
 for r in rows:r['eta']=(depart+dt.timedelta(hours=r['distance_km']/speed_kmh)).astimezone(dt.timezone.utc).isoformat().replace('+00:00','Z') if depart else None
 return rows
