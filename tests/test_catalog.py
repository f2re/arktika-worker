"""Синтетические метаданные проверяют каталог, а не точность метеопродуктов."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('catalog_under_test', ROOT/'arktika/catalog.py')
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


def asset(ch=9, identity='channel9', **changes):
    result = dict(id=identity, platform='ARCM2', time='2026-01-01T00:00:00Z', channel=ch,
                  category='channel', epsg=4326, size=100, level='L2IR',
                  filename=f'A2_20260101000000_ch{ch:02d}.tif', title='SYNTHETIC CATALOG TEST')
    result.update(changes)
    return result


def local(*channels, time='2026-01-01T00:00:00Z', platform='ARCM2'):
    return dict(id=platform+'_'+time, platform=platform, time=time,
                channels=[dict(channel=c, filename=f'LOCAL_ch{c:02d}.tif', crs='EPSG:4326') for c in channels])


class Store:
    def __init__(self, assets=(), records=(), jobs=(), state='unknown'):
        self.aa, self.rr, self.jj, self.state = list(assets), list(records), list(jobs), state
    def assets(self, day='', filters=None):
        platform=(filters or {}).get('platform')
        return [a for a in self.aa if a['time'].startswith(day) and (not platform or a['platform']==platform)]
    def records(self, day='', platform=''):
        return [r for r in self.rr if r['time'].startswith(day) and (not platform or r['platform']==platform)]
    def jobs(self):
        return self.jj
    def coverage(self, *_):
        return self.state


def app(assets=(), scenes=(), records=(), jobs=(), state='unknown'):
    return SimpleNamespace(store=Store(assets, records, jobs, state),
        scenes=lambda day='', platform='':[s for s in scenes if s['time'].startswith(day) and (not platform or s['platform']==platform)])


class InventoryTests(unittest.TestCase):
    def item(self, assets=(), scene=None, jobs=None, ch=9):
        return catalog.channel_inventory(list(assets), scene, jobs or {})[ch-1]
    def test_ten_channels_with_explicit_absence(self):
        self.assertEqual([c['channel'] for c in catalog.channel_inventory([],None,{})], list(range(1,11)))
        self.assertEqual(self.item()['state'], 'absent')
    def test_local_has_priority(self):
        c=self.item([asset()],local(9),{'channel9':dict(state='running')})
        self.assertEqual(c['state'],'local');self.assertIsNone(c['candidate_id'])
    def test_remote_not_confirmed_readable(self):
        c=self.item([asset()]);self.assertEqual(c['state'],'remote');self.assertNotIn('safe',c)
    def test_one_projection_only(self):
        c=self.item([asset(identity='mercator',epsg=3857,size=10),asset(identity='geo',epsg=4326,size=900)])
        self.assertEqual(c['candidate_id'],'geo');self.assertEqual(c['variants'],2)
    def test_active_other_projection_not_downloaded_twice(self):
        c=self.item([asset(identity='mercator',epsg=3857),asset(identity='geo')],jobs={'mercator':dict(state='running')})
        self.assertEqual(c['state'],'running');self.assertIsNone(c['candidate_id'])
    def test_queued_state(self):
        self.assertEqual(self.item([asset()],jobs={'channel9':dict(state='queued')})['state'],'queued')
    def test_paused_file_can_resume(self):
        self.assertEqual(self.item([asset()],jobs={'channel9':dict(state='paused')})['candidate_id'],'channel9')
    def test_error_can_retry(self):
        self.assertEqual(self.item([asset()],jobs={'channel9':dict(state='error')})['state'],'error')
    def test_deleted_done_file_can_download(self):
        c=self.item([asset()],jobs={'channel9':dict(state='done',path='/SYNTHETIC-MISSING-FILE')})
        self.assertEqual(c['candidate_id'],'channel9')
    def test_existing_unregistered_done_file_is_not_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'invalid.tif';p.write_bytes(b'SYNTHETIC INVALID FILE')
            c=self.item([asset()],jobs={'channel9':dict(state='done',path=str(p))})
            self.assertEqual(c['state'],'unregistered');self.assertIsNone(c['candidate_id'])
    def test_other_variant_allowed_after_unregistered(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'invalid.tif';p.write_bytes(b'SYNTHETIC')
            c=self.item([asset(),asset(identity='other',epsg=3857)],jobs={'channel9':dict(state='done',path=str(p))})
            self.assertEqual(c['candidate_id'],'other')
    def test_unknown_size_is_none(self):
        self.assertIsNone(self.item([asset(size=None)])['size'])
    def test_visible_channel_is_not_relabelled_as_ir(self):
        self.assertEqual(self.item([asset(ch=1)],ch=1)['state'],'remote')
    def test_rgb_not_a_numeric_channel(self):
        self.assertEqual(self.item([asset(category='rgb')])['state'],'absent')
    def test_no_paths_or_uris_in_summary(self):
        a=asset(uri='https://example.invalid/SECRET',self='PRIVATE')
        s=catalog.describe_session([a],local(9),{})
        self.assertNotIn('SECRET',str(s));self.assertNotIn('PRIVATE',str(s))
    def test_local_filenames_searchable(self):
        self.assertIn('LOCAL_ch09.tif',catalog.describe_session([],local(9),{})['search_text'])
    def test_no_mutation(self):
        aa=[asset()];ll=local(9);before=copy.deepcopy((aa,ll))
        catalog.describe_session(aa,ll,{})
        self.assertEqual((aa,ll),before)


class SessionsTests(unittest.TestCase):
    def run_catalog(self, obj=None, **filters):
        return catalog.catalog_sessions(obj or app(), '2026-01-01', dict(category='all', **filters))
    def test_empty_not_marked_complete(self):
        r=self.run_catalog();self.assertEqual(r['total'],0);self.assertEqual(r['coverage'],'unknown')
    def test_complete_empty_distinct(self):
        self.assertEqual(self.run_catalog(app(state='complete'))['coverage'],'complete')
    def test_local_only_included(self):
        r=self.run_catalog(app(scenes=[local(4,9)]));self.assertEqual(r['total'],1);self.assertEqual(r['sessions'][0]['local_channels'],[4,9])
    def test_local_and_remote_merged(self):
        r=self.run_catalog(app([asset()],[local(9)]));self.assertEqual(r['total'],1);self.assertEqual(r['sessions'][0]['count'],1)
    def test_no_uri_record_included(self):
        r=self.run_catalog(app(records=[dict(id='record',time=asset()['time'],platform='ARCM2',level='L1')]))
        self.assertTrue(r['sessions'][0]['no_uri']);self.assertEqual(r['sessions'][0]['record_count'],1)
    def test_two_platforms_same_time_separate(self):
        r=self.run_catalog(app([asset(),asset(identity='other',platform='ARCM1')]))
        self.assertEqual(r['total'],2)
    def test_platform_filter(self):
        r=self.run_catalog(app([asset(),asset(identity='other',platform='ARCM1')]),platform='ARCM1')
        self.assertEqual(r['total'],1);self.assertEqual(r['sessions'][0]['platform'],'ARCM1')
    def test_invalid_platform(self):
        with self.assertRaises(ValueError):self.run_catalog(platform='unknown')
    def test_channel_filter_local(self):
        self.assertEqual(self.run_catalog(app(scenes=[local(4)]),channel=4)['total'],1)
        self.assertEqual(self.run_catalog(app(scenes=[local(4)]),channel=9)['total'],0)
    def test_filter_requires_same_file(self):
        obj=app([asset(ch=4,epsg=4326),asset(ch=9,identity='nine',epsg=3857)])
        self.assertEqual(self.run_catalog(obj,channel=9,epsg=4326)['total'],0)
    def test_search_tokens(self):
        r=self.run_catalog(app([asset(title='Проверка источника')]),query='ПРОВЕРКА L2IR')
        self.assertEqual(r['total'],1)
    def test_raw_record_search(self):
        rec=dict(id='passport-123',time=asset()['time'],platform='ARCM2',level='L1')
        self.assertEqual(self.run_catalog(app(records=[rec]),query='passport-123')['total'],1)
    def test_invalid_date(self):
        with self.assertRaises(ValueError):catalog.catalog_sessions(app(),'2026-02-30',{})
    def test_pagination_total_no_truncation(self):
        aa=[asset(identity=str(i),time=f'2026-01-01T{i//60:02d}:{i%60:02d}:00Z') for i in range(85)]
        obj=app(aa)
        a=catalog.catalog_sessions(obj,'2026-01-01',{},0,60)
        b=catalog.catalog_sessions(obj,'2026-01-01',{},60,60)
        self.assertEqual(a['total'],85);self.assertEqual(len(a['sessions'])+len(b['sessions']),85)
        self.assertEqual(len({s['time'] for s in a['sessions']+b['sessions']}),85)
    def test_filtered_ids_do_not_include_other_channels(self):
        obj=app([asset(ch=4,identity='four'),asset(ch=9,identity='nine')])
        r=self.run_catalog(obj,channel=9)
        self.assertEqual(r['ids'],['nine']);self.assertEqual(r['files'],1)
        self.assertEqual(r['sessions'][0]['ids'],['nine'])
    def test_preview_contract_preserved(self):
        r=self.run_catalog(app([asset(),asset(ch=9,identity='preview',category='image')]))
        self.assertEqual(r['sessions'][0]['preview'],'preview')
        self.assertEqual(r['sessions'][0]['preview_caption'],'Канал 9')
    def test_no_temperature_claim_in_readiness(self):
        r=self.run_catalog(app([asset()]))
        self.assertNotIn('calibrated',str(r));self.assertNotIn('kelvin',str(r))


if __name__=='__main__':unittest.main()
