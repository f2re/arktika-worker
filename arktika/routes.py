"""Наблюдательный профиль маршрута на исходных пикселях; ETA не является прогнозом."""
import datetime as dt
import numpy as np
import rasterio
from pyproj import Transformer
from .geo import densify_route
from .processing import calibration,CalibrationError

def route_profile(scene,points,cal,step_km=25,speed_kmh=300,departure='',max_age_minutes=60):
 rows=densify_route(points,step_km,speed_kmh,departure);units={};obs=dt.datetime.fromisoformat(scene['time'].replace('Z','+00:00'))
 for ch,entry in scene['channels'].items():
  with rasterio.open(entry['path']) as ds:
   if not ds.crs:raise ValueError('В исходнике нет проекции.')
   tr=Transformer.from_crs(4326,ds.crs,always_xy=True);coords=[tr.transform(r['lon'],r['lat']) for r in rows]
   try:scale,offset,unit,status,ref=calibration(ds,int(ch),cal)
   except CalibrationError:scale,offset,unit,status,ref=1.,0.,'DN','unknown','Нет коэффициентов этого канала'
   units[ch]=dict(unit=unit,calibration=status)
   for r,c,v in zip(rows,coords,ds.sample(coords,indexes=1,masked=True)):
    inside=ds.bounds.left<=c[0]<ds.bounds.right and ds.bounds.bottom<c[1]<=ds.bounds.top
    valid=inside and not np.ma.is_masked(v[0]) and np.isfinite(float(v[0]));r.setdefault('channels',{})[ch]=float(v[0])*scale+offset if valid else None
 for r in rows:
  eta=dt.datetime.fromisoformat(r['eta'].replace('Z','+00:00')) if r['eta'] else obs;age=(eta-obs).total_seconds()/60
  r.update(observation_time=scene['time'],observation_age_minutes=age,forecast=False,missing=all(v is None for v in r['channels'].values()))
  r['status']='Нет данных' if r['missing'] else ('Снимок позднее прохождения' if age<0 else ('Снимок устарел к прохождению' if age>max_age_minutes else 'Наблюдение, не оценка безопасности'))
 features=[dict(type='Feature',geometry=dict(type='Point',coordinates=[r['lon'],r['lat']]),properties={k:v for k,v in r.items() if k not in ('lon','lat')}) for r in rows]
 return dict(scene_id=scene['id'],time=scene['time'],units=units,samples=rows,length_km=rows[-1]['distance_km'],points=points,geojson=dict(type='FeatureCollection',features=features,source_scene=scene['id'],observation_time=scene['time'],channel_units=units,forecast=False),note='Один срок наблюдения на исходной сетке GeoTIFF. ETA не превращает снимок в прогноз. Ветер, видимость, высота, водность и безопасность маршрута здесь не определяются.')
