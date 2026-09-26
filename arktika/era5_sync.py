"""Пространственно-временное сопоставление ERA5 и исходных DN.

Безоблачная открытая вода — модельно отобранные опоры, а не измеренный эталон.
Нет экстраполяции во времени, под землёй или за границами ERA5.
"""
from __future__ import annotations
import datetime as dt
import math
from collections import Counter
from pathlib import Path
import numpy as np
import rasterio
from rasterio.windows import Window
from pyproj import Transformer
from .era5_access import cancelled
from .geo import solar_elevation
from .interpretation import utc

ALIASES={'t':['t','temperature','T'],'q':['q','specific_humidity','Q'],
         'o3':['o3','ozone_mass_mixing_ratio','O3'],'z':['z','geopotential','Z'],
         'skt':['skt','skin_temperature','SKT'],'sp':['sp','surface_pressure','SP'],
         't2m':['t2m','2t','2m_temperature','VAR_2T'],'d2m':['d2m','2d','2m_dewpoint_temperature','VAR_2D'],
         'u10':['u10','10u','10m_u_component_of_wind','VAR_10U'],
         'v10':['v10','10v','10m_v_component_of_wind','VAR_10V'],
         'tcc':['tcc','total_cloud_cover','TCC'],'siconc':['siconc','ci','sea_ice_cover','CI'],
         'lsm':['lsm','land_sea_mask','LSM']}
UNITS={'t':{'K','kelvin'},'q':{'kg kg**-1','kg kg-1','kg/kg','1'},
       'o3':{'kg kg**-1','kg kg-1','kg/kg'},'z':{'m**2 s**-2','m2 s-2','m^2 s^-2','m2/s2'},
       'skt':{'K','kelvin'},'sp':{'Pa','pascal'},'t2m':{'K','kelvin'},'d2m':{'K','kelvin'},
       'u10':{'m s**-1','m s-1','m/s'},'v10':{'m s**-1','m s-1','m/s'},
       'tcc':{'(0 - 1)','(0-1)','1','0-1','~'},'siconc':{'(0 - 1)','(0-1)','1','0-1','~'},
       'lsm':{'(0 - 1)','(0-1)','1','0-1','~'}}


def normalize_dataset(ds,group):
    required=['t','q','o3','z'] if group=='pressure' else ['skt','sp','t2m','d2m','u10','v10','tcc','siconc','lsm']
    renames={}
    for target,names in [('latitude',['lat']),('longitude',['lon']),('time',['valid_time']),('level',['pressure_level','isobaric'])]:
        if target not in ds.coords:
            candidates=[name for name in names if name in ds.coords]
            if len(candidates)==1: renames[candidates[0]]=target
    ds=ds.rename(renames)
    for key in ('latitude','longitude','time'):
        if key not in ds.coords or ds[key].ndim!=1: raise ValueError('ERA5: нет одномерной координаты '+key)
    if 'expver' in ds.dims:
        versions=list(ds.expver.values)
        if any(int(v) not in (1,5) for v in versions): raise ValueError('ERA5: неизвестная версия анализа expver.')
        a=ds.sel(expver=1,drop=True) if 1 in versions else ds.sel(expver=5,drop=True)
        if 1 in versions and 5 in versions: a=a.combine_first(ds.sel(expver=5,drop=True))
        ds=a
    renames={}
    for key in required:
        found=[k for k in ALIASES[key] if k in ds.data_vars]
        if len(found)!=1: raise ValueError('ERA5: нужна ровно одна переменная '+key)
        renames[found[0]]=key
    ds=ds.rename(renames)[required]
    for key in required:
        unit=ds[key].attrs.get('units','').strip()
        if unit not in UNITS[key]: raise ValueError('ERA5: неподтверждённые единицы '+key)
        if not set(ds[key].dims)<= {'time','latitude','longitude','level'}:
            raise ValueError('ERA5: неожиданные измерения переменной '+key)
    if group=='pressure':
        if 'level' not in ds.coords: raise ValueError('ERA5: отсутствуют изобарические уровни.')
        unit=ds.level.attrs.get('units','').lower()
        if unit not in ('hpa','millibars','millibar','mb'): raise ValueError('ERA5: давление уровней должно быть в гПа.')
        from .era5_access import LEVELS
        if not np.array_equal(np.sort(np.asarray(ds.level,float)),LEVELS): raise ValueError('ERA5: запрошены все 37 изобарических уровней 1–1000 гПа.')
    if not np.issubdtype(ds.time.dtype,np.datetime64): raise ValueError('ERA5: время NetCDF не декодировано.')
    for key in ('latitude','longitude'):
        if not np.isfinite(ds[key]).all(): raise ValueError('ERA5: неверные координаты.')
    return ds


def load_group(files,group,plan):
    import xarray as xr
    chunks=[];west=plan['area'][1]
    for item in files:
        if item['group']!=group: continue
        with xr.open_dataset(item['path']) as src:
            ds=normalize_dataset(src,group)
            expected=np.datetime64(utc(item['time']).replace(tzinfo=None))
            if ds.sizes['time']!=1 or ds.time.values[0]!=expected:
                raise ValueError('ERA5: источник вернул не запрошенный час; ближайший срок не подставляется.')
            # Непрерывная долгота относительно запрошенного запада; подходит для линии 180°.
            lon=west+((np.asarray(ds.longitude,float)-west)%360)
            ds=ds.assign_coords(longitude=lon).sortby('longitude').sortby('latitude')
            ds=ds.drop_duplicates('longitude')
            chunks.append(ds.load())
    if not chunks: raise ValueError('ERA5: нет данных группы '+group)
    ds=xr.combine_by_coords(chunks,combine_attrs='drop_conflicts',data_vars='all')
    ds=ds.sortby('time').sortby('latitude').sortby('longitude')
    if group=='pressure': ds=ds.sortby('level')
    times=np.asarray([np.datetime64(utc(s).replace(tzinfo=None)) for s in plan['times']])
    if not np.array_equal(ds.time.values,times): raise ValueError('ERA5: неполная пара сроков.')
    return ds


def interpolate_time(ds,plan):
    when=np.datetime64(utc(plan['time']).replace(tzinfo=None))
    if not ds.time.values[0]<=when<=ds.time.values[-1]: raise ValueError('ERA5: запрещена экстраполяция времени.')
    if len(ds.time)==1: return ds.isel(time=0,drop=True)
    weight=plan['time_weight']
    return ds.isel(time=0,drop=True)*(1-weight)+ds.isel(time=1,drop=True)*weight


def profile_at(pl,sfc,lat,lon,when,zenith):
    """Оба набора на сетке 0,25°; извлекается именно узел ERA5, не соседний профиль."""
    pp=pl.sel(latitude=lat,longitude=lon)
    ss=sfc.sel(latitude=lat,longitude=lon)
    values={k:float(ss[k]) for k in ('skt','sp','t2m','d2m','u10','v10','tcc','siconc','lsm')}
    if not all(math.isfinite(v) for v in values.values()): raise ValueError('surface_missing')
    if not (80000<=values['sp']<=110000 and 250<=values['skt']<=325 and 190<=values['d2m']<=values['t2m']+1 and 190<=values['t2m']<=340):
        raise ValueError('surface_range')
    if abs(values['u10'])>100 or abs(values['v10'])>100: raise ValueError('surface_range')
    pressure=np.asarray(pp.level,float);mask=pressure<values['sp']/100
    p=pressure[mask];t=np.asarray(pp.t,float)[mask];q=np.asarray(pp.q,float)[mask];o3=np.asarray(pp.o3,float)[mask];z=np.asarray(pp.z,float)[mask]
    if len(p)<30 or p[0]!=1 or p[-1]<925: raise ValueError('pressure_coverage')
    if not all(np.isfinite(v).all() for v in (p,t,q,o3,z)): raise ValueError('profile_missing')
    if np.any((t<120)|(t>340)) or np.any((q<0)|(q>.1)) or np.any((o3<0)|(o3>1e-3)):
        raise ValueError('profile_range')
    # Magnus над жидкой водой: только вспомогательная влажность на 2 м.
    td=values['d2m']-273.15;e=611.2*np.exp(17.67*td/(td+243.5))
    q2=.622*e/(values['sp']-.378*e)
    if not 0<q2<.1: raise ValueError('surface_humidity')
    date=utc(when)
    return {'lat':lat,'lon':(lon+180)%360-180,'pressure_hpa':p.tolist(),'temperature_k':t.tolist(),
            'humidity_kg_kg':q.tolist(),'ozone_kg_kg':o3.tolist(), 'geopotential_m2_s2':z.tolist(),
            'skin_k':values['skt'],'sp_pa':values['sp'],'t2m_k':values['t2m'],'q2m_kg_kg':float(q2),
            'u10':values['u10'],'v10':values['v10'],'zenith_deg':zenith,
            'sun_zenith_deg':90-float(solar_elevation((lon+180)%360-180,lat,when)),
            'datetime':[date.year,date.month,date.day,date.hour,date.minute,date.second]}


def collocate(scene,plan,files,zenith,cancel=None,max_samples=384):
    """Одна опора на ячейку ERA5. Соседние узлы группируются для проверки."""
    pl=load_group(files,'pressure',plan);sf=load_group(files,'surface',plan)
    profiles=[];dns=[];groups=[];rejected=Counter();opened={}
    try:
        if not np.array_equal(pl.latitude,sf.latitude) or not np.array_equal(pl.longitude,sf.longitude):
            raise ValueError('Сетки атмосферных и поверхностных полей ERA5 не совпадают.')
        # Условия проверяются на обоих сроках, не только по интерполированному среднему.
        valid=((sf.lsm>=0)&(sf.lsm<=.01)&(sf.siconc>=0)&(sf.siconc<=.01)&(sf.tcc>=0)&(sf.tcc<=.01)).all('time')
        atm=interpolate_time(pl,plan);surface=interpolate_time(sf,plan)
        candidates=np.argwhere(np.asarray(valid.transpose('latitude','longitude'),bool))
        rejected['model_cloud_land_ice_or_missing']=int(valid.size-len(candidates))
        # Детерминированное разрежение до ограниченного числа профилей.
        if len(candidates)>max_samples:
            ids=np.linspace(0,len(candidates)-1,max_samples,dtype=int);candidates=candidates[ids]
        for ch in plan['channels']:
            entry=scene['channels'].get(str(ch))
            if not entry: raise ValueError('Не скачан канал '+str(ch))
            ds=rasterio.open(entry['path'])
            if ds.count!=1 or not ds.crs: ds.close();raise ValueError('Для опор нужен одноканальный GeoTIFF с CRS.')
            opened[ch]=(ds,Transformer.from_crs(4326,ds.crs,always_xy=True))
        for i,j in candidates:
            cancelled(cancel);lat=float(valid.latitude[i]);lon=float(valid.longitude[j]);lon_geo=(lon+180)%360-180
            if 4 in plan['channels'] and float(solar_elevation(lon_geo,lat,plan['time']))>-6:
                rejected['channel4_not_night']+=1;continue
            try: profile=profile_at(atm,surface,lat,lon,plan['time'],zenith)
            except ValueError as exc: rejected[str(exc)]+=1;continue
            row=[]
            for ch in plan['channels']:
                ds,tr=opened[ch];x,y=tr.transform(lon_geo,lat)
                if not np.isfinite(x+y): break
                r,c=ds.index(x,y)
                if not (1<=r<ds.height-1 and 1<=c<ds.width-1): break
                a=ds.read(1,window=Window(c-1,r-1,3,3),masked=True)
                v=np.asarray(a.compressed(),float)
                if len(v)!=9 or not np.isfinite(v).all(): break
                # Проверяется относительная неоднородность DN, не «облачность по температуре».
                if np.ptp(v)>max(abs(float(np.median(v)))*.03,2.): break
                row.append(float(np.mean(v)))
            if len(row)!=len(plan['channels']): rejected['satellite_missing_or_texture']+=1;continue
            profiles.append(profile);dns.append(row);groups.append(str(math.floor(lat/2))+'/'+str(math.floor(lon/2)))
        return {'profiles':profiles,'dn':dns,'groups':groups,'rejected':dict(rejected),
                'selection':'ERA5 open water, tcc<=0.01 at both bracketing hours; 3x3 DN texture',
                'cloud_mask_independent':False,'candidate_limit':max_samples}
    finally:
        pl.close();sf.close()
        for ds,tr in opened.values(): ds.close()


def fit_reference(dn,bt,groups):
    """Линейная эмпирическая BT-шкала; пространственно-групповая проверка."""
    x=np.asarray(dn,float);y=np.asarray(bt,float);g=np.asarray(groups)
    if x.ndim!=1 or x.shape!=y.shape or len(g)!=len(x) or len(x)<24:
        raise ValueError('Не менее 24 пригодных опор для канала.')
    if not np.isfinite(x+y).all() or np.any((y<120)|(y>400)) or np.ptp(x)<=0 or np.ptp(y)<10:
        raise ValueError('Недостаточный диапазон опор: требуется не менее 10 K. Тёплый океан не задаёт шкалу холодных вершин.')
    unique=np.unique(g)
    if len(unique)<4: raise ValueError('Нужно не менее четырёх независимых пространственных групп.')
    # Масштабирование предотвращает потерю точности при большом смещении DN.
    def solve(xx,yy):
        center=float(xx.mean());spread=float(xx.std())
        if spread<1e-9 or np.ptp(yy)<1: raise ValueError('В группе недостаточно диапазона.')
        a=(xx-center)/spread;design=np.column_stack((a,np.ones(len(a))))
        beta=np.linalg.lstsq(design,yy,rcond=None)[0]
        for _ in range(12):
            residual=yy-design@beta;robust=max(.25,1.4826*np.median(abs(residual-np.median(residual))))
            weights=np.minimum(1,1.5*robust/np.maximum(abs(residual),1e-12))
            beta=np.linalg.lstsq(design*np.sqrt(weights[:,None]),yy*np.sqrt(weights),rcond=None)[0]
        return float(beta[0]/spread),float(beta[1]-beta[0]*center/spread)
    a,b=solve(x,y);pred=a*x+b;cv=np.empty(len(x))
    # Не более 8 пространственных блоков, детерминированно; ни одна опора не в своей подгонке.
    fold={name:i%min(8,len(unique)) for i,name in enumerate(sorted(unique))}
    for k in sorted(set(fold.values())):
        held=np.asarray([fold[name]==k for name in g]);train=~held
        if train.sum()<12: raise ValueError('Недостаточно опор для отложенной проверки.')
        aa,bb=solve(x[train],y[train]);cv[held]=aa*x[held]+bb
    rmse=float(np.sqrt(np.mean((pred-y)**2)));validation=float(np.sqrt(np.mean((cv-y)**2)))
    if not .000001<=abs(a)<=10000 or validation>2 or abs(float((cv-y).mean()))>1:
        raise ValueError('Модельно-опорная шкала не прошла проверку (RMSE ≤ 2 K, смещение ≤ 1 K).')
    return {'scale':a,'offset':b,'units':'K','status':'assumed','method':'era5_electrol_proxy',
            'reference':'ERA5 → RTTOV 13.2 / МСУ-ГС Электро-Л №2; исследовательский спектральный аналог',
            'valid_dn':[float(x.min()),float(x.max())],'valid_temperature_k':[float(y.min()),float(y.max())],
            'enforce_valid_dn':True,'fit_rmse_k':rmse,'group_cv_rmse_k':validation,'n':len(x),'groups':len(unique),
            'validation':'model_reference_spatial_cv_not_instrument_accuracy',
            'plot':[{'dn':float(xx),'reference_k':float(yy),'fit_k':float(zz)} for xx,yy,zz in zip(x[::max(1,len(x)//100)],y[::max(1,len(x)//100)],pred[::max(1,len(x)//100)])]}
