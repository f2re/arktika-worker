"""PNG/изолинии/ветер на той же сетке, что и спутник; исходники не изменяются."""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
from PIL import Image
from pyproj import Geod, Transformer
from scipy.spatial import cKDTree
from .catalog import VARIABLES

GEOD=Geod(ellps='WGS84')
R=6371.0088
PALETTES={
    'temperature':['#243996','#55a7cf','#f7f7ed','#f8b365','#b2182b'],
    'diverging':['#2166ac','#92c5de','#f7f7f7','#f4a582','#b2182b'],
    'pressure':['#3b4cc0','#9ebeff','#f5f5f5','#f3a481','#b40426'],
    'humidity':['#faf7ec','#ccebc5','#7bccc4','#2b8cbe','#084081'],
    'wind':['#f7fcf0','#ccebc5','#7bccc4','#2b8cbe','#54278f'],
    'height':['#f7fcf5','#c7e9c0','#74c476','#238b45','#00441b'],
}


def sphere(lon,lat):
    lon=np.deg2rad(lon);lat=np.deg2rad(lat);c=np.cos(lat)
    return np.column_stack((c.ravel()*np.cos(lon).ravel(),c.ravel()*np.sin(lon).ravel(),np.sin(lat).ravel()))


def sampling(ds,lon,lat):
    """Nearest coordinate, NOT nearest nonmissing value. Never fill nodata holes."""
    la=ds.latitude.values;lo=ds.longitude.values
    valid=np.isfinite(la)&np.isfinite(lo)&(np.abs(la)<=90)
    positions=np.flatnonzero(valid.ravel())
    if not positions.size: raise ValueError('Нет валидной геометрии поля.')
    tree=cKDTree(sphere(lo[valid],la[valid]))
    lon,lat=np.broadcast_arrays(lon,lat);good=np.isfinite(lon)&np.isfinite(lat)&(np.abs(lat)<=90)
    safe_lon=np.where(good,lon,0);safe_lat=np.where(good,lat,0)
    dist,index=tree.query(sphere(safe_lon,safe_lat),workers=1)
    # Radius derives from native cell spacing, not visual-map resolution.
    nominal=float(ds.attrs['resolution_km'])
    steps=[]
    for axis in range(2):
        if la.shape[axis]>1:
            a=[slice(None),slice(None)];b=a.copy();a[axis]=slice(None,-1);b[axis]=slice(1,None)
            _,_,d=GEOD.inv(lo[tuple(a)],la[tuple(a)],lo[tuple(b)],la[tuple(b)])
            d=np.asarray(d)/1000.;d=d[np.isfinite(d)&(d>0)&(d<nominal*5)]
            if d.size: steps.append(float(np.median(d)))
    radius=.85*max(steps or [nominal])
    distances=2*R*np.arcsin(np.clip(dist.reshape(lon.shape)/2,0,1))
    good &= (distances<=radius)
    n,w,s,e=json.loads(ds.attrs['area'])
    wrap=(safe_lon+180)%360-180
    good &= (safe_lat>=s)&(safe_lat<=n)
    good &= ((wrap>=w)&(wrap<=e)) if w<e else ((wrap>=w)|(wrap<=e))
    indices=positions[index]
    out={}
    for key in ('value','u','v'):
        if key in ds: out[key]=np.where(good,ds[key].values.ravel()[indices].reshape(lon.shape),np.nan)
    out['distance_km']=np.where(good,distances,np.nan)
    return out


def display_grid(g):
    a=g['transform'];xx=a.c+(np.arange(g['width'])+.5)*a.a;yy=a.f+(np.arange(g['height'])+.5)*a.e
    lon,lat=Transformer.from_crs(g['crs'],4326,always_xy=True).transform(*np.meshgrid(xx,yy))
    return lon,lat


def limits(ds,options):
    values=ds.value.values;v=values[np.isfinite(values)]
    if not v.size: raise ValueError('Нет значений для цветовой шкалы.')
    difference=bool(ds.attrs.get('difference_of'))
    key=ds.attrs['variable'];spec=VARIABLES[key]
    lo,hi=spec.limits
    if key=='z': lo,hi=np.quantile(v,[.02,.98]);hi=max(hi,lo+1.)
    if difference:
        hi=max(float(np.quantile(abs(v),.98)),.1);lo=-hi
    if options.get('min') is not None: lo=float(options['min'])
    if options.get('max') is not None: hi=float(options['max'])
    if not np.isfinite(lo+hi) or not lo<hi: raise ValueError('Нижняя граница шкалы должна быть меньше верхней.')
    return float(lo),float(hi)


def color_image(values,low,high,palette):
    stops=PALETTES[palette];rgb=np.array([[int(c[k:k+2],16) for k in (1,3,5)] for c in stops])
    ratio=np.clip((np.nan_to_num(values,nan=low)-low)/(high-low),0,1)
    rgba=np.empty(values.shape+(4,),dtype='uint8')
    for k in range(3): rgba[...,k]=np.interp(ratio,np.linspace(0,1,len(rgb)),rgb[:,k]).astype('uint8')
    rgba[...,3]=np.where(np.isfinite(values),255,0)
    return rgba


def render(ds,g,target,options=None):
    import contourpy
    options=options or {};target=Path(target);target.mkdir(parents=True,exist_ok=True)
    lon,lat=display_grid(g);sample=sampling(ds,lon,lat);values=sample['value']
    low,high=limits(ds,options);spec=VARIABLES[ds.attrs['variable']]
    palette='diverging' if ds.attrs.get('difference_of') else spec.palette
    Image.fromarray(color_image(values,low,high,palette)).save(target/'map.png')
    contours=[]
    if np.isfinite(values).any():
        generator=contourpy.contour_generator(x=np.arange(g['width'])+.5,y=np.arange(g['height'])+.5,z=np.ma.masked_invalid(values),corner_mask=False)
        count=0
        for level in np.linspace(low,high,11):
            for line in generator.lines(float(level)):
                if len(line)<3: continue
                count+=len(line)
                if count>150_000: break
                contours.append({'value':round(float(level),3),'points':np.round(line,2).tolist()})
            if count>150_000: break
    vectors=[]
    if 'u' in sample:
        to_map=Transformer.from_crs(4326,g['crs'],always_xy=True);a=g['transform']
        stride=max(24,int(g['width']/26))
        for y in range(stride//2,g['height'],stride):
            for x in range(stride//2,g['width'],stride):
                u=float(sample['u'][y,x]);v=float(sample['v'][y,x])
                if not np.isfinite(u+v): continue
                speed=float(np.hypot(u,v))
                if speed<.1: continue
                az=float(np.degrees(np.arctan2(u,v)))
                lon2,lat2,_=GEOD.fwd(lon[y,x],lat[y,x],az,10000.)
                mx,my=to_map.transform(lon2,lat2);dx=(mx-a.c)/a.a-(x+.5);dy=(my-a.f)/a.e-(y+.5)
                length=np.hypot(dx,dy)
                if not np.isfinite(length) or length<1e-9: continue
                vectors.append({'x':x+.5,'y':y+.5,'dx':float(dx/length*16),'dy':float(dy/length*16),'speed':round(speed,1)})
    return {'grid':{k:v for k,v in g.items() if k!='transform'},'contours':contours,'vectors':vectors,
            'legend':{'min':low,'max':high,'unit':spec.unit,'colors':PALETTES[palette],
                      'title':spec.title,'nodata':'Прозрачно: нет данных; не нулевое значение.',
                      'sampling':'Ближайший исходный узел. Масштаб карты не повышает разрешение.',
                      'arrows':'Направление движения воздуха; длина условная. Скорость — в м/с.'},
            'valid_pixels':int(np.isfinite(values).sum())}


def difference(first,second,first_id,second_id):
    """A−B on the COARSER native grid, not on an upsampled fine model grid."""
    import xarray as xr
    for name in ('variable','time','level','unit','time_kind'):
        if first.attrs.get(name)!=second.attrs.get(name): raise ValueError('Для разности должны совпадать поле, срок, уровень, единицы и тип времени: '+name)
    if first.attrs.get('difference_of') or second.attrs.get('difference_of'): raise ValueError('Выберите два исходных поля, не уже рассчитанные разности.')
    coarse=max((first,second),key=lambda d:float(d.attrs['resolution_km']))
    lon=coarse.longitude.values;lat=coarse.latitude.values
    a=sampling(first,lon,lat)['value'];b=sampling(second,lon,lat)['value'];value=a-b
    if not np.isfinite(value).any(): raise ValueError('У полей нет совместной области с данными.')
    attrs=dict(coarse.attrs,source='difference',difference_of=json.dumps([first_id,second_id]),
               source_label=first.attrs.get('source')+' − '+second.attrs.get('source'),
               comparison='nearest_on_coarser_native_grid; no blending; intersection of masks')
    result=xr.Dataset({'value':(('y','x'),value.astype('float32')),
                       'latitude':(('y','x'),lat),'longitude':(('y','x'),lon)},attrs=attrs).set_coords(['latitude','longitude'])
    for name in ('latitude','longitude','value'): result[name].attrs=dict(coarse[name].attrs)
    result=result.assign_coords({k:v for k,v in coarse.coords.items() if v.ndim==0})
    result.value.attrs.pop('standard_name',None)
    result.value.attrs['long_name']='A minus B: '+VARIABLES[attrs['variable']].title
    if attrs['variable'] in ('t','t2m','sst'): result.value.attrs['units']='K'
    result.value.attrs['comment']='Difference, not an absolute quantity. Temperature increments in K equal increments in degrees Celsius.'
    return result
