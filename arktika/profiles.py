"""Профильные диагностические расчёты. Не восстановление из спутникового снимка."""
import numpy as np
import datetime as dt

def integrate(data):
 if data.get('pressure_units')!='Pa' or data.get('humidity_units')!='kg/kg':raise ValueError('Обязательны pressure_units=Pa и humidity_units=kg/kg.')
 if not isinstance(data.get('source'),str) or not data['source'].strip():raise ValueError('Укажите источник профиля.')
 try:stamp=dt.datetime.fromisoformat(data['valid_time'].replace('Z','+00:00'))
 except (KeyError,AttributeError,TypeError,ValueError):raise ValueError('Нужен срок профиля ISO 8601 с часовым поясом.')
 if stamp.tzinfo is None:raise ValueError('Нужен часовой пояс срока профиля.')
 p=np.asarray(data['pressure'],float);q=np.asarray(data['specific_humidity'],float)
 if p.ndim!=1 or len(p)<2 or p.shape!=q.shape or not np.isfinite(p).all() or not np.isfinite(q).all():raise ValueError('Неверная структура профиля.')
 if (p<=0).any() or (p>110000).any() or (q<0).any() or (q>.1).any():raise ValueError('Проверьте единицы давления и влажности.')
 if not ((np.diff(p)>0).all() or (np.diff(p)<0).all()):raise ValueError('Давление должно быть строго монотонным.')
 idx=np.argsort(p);p=p[idx];q=q[idx]
 def integ(a):return float(np.sum((a[:-1]+a[1:])*.5*np.diff(p))/9.80665)
 result=dict(source=data['source'],valid_time=data['valid_time'],pressure_range_pa=[float(p[0]),float(p[-1])],vapor_kg_m2=integ(q),vapor_mm=integ(q),note='Интеграл в заданных пределах профиля, не обязательно полный столб атмосферы; не спутниковое восстановление.')
 for key,name in [('cloud_liquid','liquid_water_kg_m2'),('cloud_ice','ice_water_kg_m2')]:
  if key in data:
   if data.get('condensate_units')!='kg/kg':raise ValueError('Для конденсата обязательны condensate_units=kg/kg.')
   a=np.asarray(data[key],float)
   if a.shape!=q.shape or not np.isfinite(a).all() or (a<0).any() or (a>.1).any():raise ValueError('Проверьте профиль конденсата, кг/кг.')
   result[name]=integ(a[idx])
 if ('u' in data)!=('v' in data):raise ValueError('Нужны обе компоненты ветра u и v.')
 if 'u' in data or 'v' in data:
  if data.get('wind_units')!='m/s':raise ValueError('wind_units=m/s обязательно.')
  u=np.asarray(data['u'],float);v=np.asarray(data['v'],float)
  if u.shape!=q.shape or v.shape!=q.shape or not np.isfinite(u).all() or not np.isfinite(v).all():raise ValueError('Ветер должен быть задан на всех уровнях.')
  x,y=integ(q*u[idx]),integ(q*v[idx]);result.update(ivt_u_kg_m_s=x,ivt_v_kg_m_s=y,ivt_kg_m_s=float(np.hypot(x,y)))
 return result

def cloud_top(data):
 if data.get('cloud_confirmed') is not True:raise ValueError('Нужно независимое подтверждение облака, а не холодный порог.')
 if data.get('temperature_units')!='K' or data.get('height_units')!='m':raise ValueError('Единицы: K и m.')
 z=np.asarray(data['height'],float);t=np.asarray(data['temperature'],float);bt=float(data['brightness_temperature'])
 if z.ndim!=1 or not 2<=len(z)<=4096 or z.shape!=t.shape or not np.isfinite(z).all() or not np.isfinite(t).all() or not np.isfinite(bt) or not (np.diff(z)>0).all():raise ValueError('Некорректный профиль T(z).')
 if np.any(t<120) or np.any(t>340) or not 120<=bt<=400 or np.any(z<-500) or np.any(z>35000):raise ValueError('Проверьте единицы и диапазон T(z).')
 roots=[];flat=[]
 for i in range(len(z)-1):
  if t[i]==t[i+1]==bt:flat.append([float(z[i]),float(z[i+1])]);continue
  if t[i]==bt:roots.append(float(z[i]));continue
  if t[i+1]==bt:roots.append(float(z[i+1]));continue
  if t[i]!=t[i+1] and min(t[i],t[i+1])<bt<max(t[i],t[i+1]):roots.append(float(z[i]+(bt-t[i])/(t[i+1]-t[i])*(z[i+1]-z[i])))
 roots=sorted(set(roots))
 return dict(candidate_heights_m=roots,ambiguous_layers_m=flat,has_solution=bool(roots or flat),ambiguous=len(roots)>1 or bool(flat),note='Сопоставление Tя с профилем, без поправки на прозрачность облака и атмосферное поглощение. Не полноценная карта ВГО.')
