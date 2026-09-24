"""Synthetic fixtures exercise arithmetic and refusal gates, not meteorological skill."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
from arktika.interpretation import (spectral, normalize_profile, parse_profile, profile_diagnostics,
                                   height_ranges, applicability, phase_field, GUIDES)
from arktika.workstation import Workstation
from arktika.processing import build_product
from arktika.geo import project_points
from test_science import fixture


def pixel(values=None, unit='K', sun=-15, time='2026-01-01T00:00:00Z'):
    values=values or {4:263,5:235,7:264,9:268,10:267}
    return dict(lon=30,lat=70,time=time,sun_elevation=sun,
                channels=[dict(channel=k,value=v,unit=unit,calibration='assumed') for k,v in values.items()])


def profile(**overrides):
    result=dict(source='SYNTHETIC UNIT TEST ONLY',valid_time='2026-01-01T00:00:00Z',lat=70,lon=30,
                height_units='m',temperature_units='K',height=[0,1000,2000,3000],temperature=[280,274,268,262],
                pressure=[100000,89000,79000,70000],pressure_units='Pa',humidity_units='kg/kg',
                specific_humidity=[.004,.003,.002,.001],cloud_liquid=[0,.0001,.0002,0],
                condensate_units='kg/kg',radius_km=100,max_hours=3)
    result.update(overrides)
    return normalize_profile(result)


class InterpretationTests(unittest.TestCase):
    def test_dn_produces_no_physical_metrics(self):
        r=spectral(pixel(unit='DN'));self.assertEqual(r['metrics'],[]);self.assertEqual(r['phase'],'unknown')
    def test_signed_differences(self):
        r=spectral(pixel());d={m['id']:m['value'] for m in r['metrics']}
        self.assertEqual(d['d109'],-1);self.assertEqual(d['d97'],4);self.assertEqual(d['d94'],5)
        self.assertAlmostEqual(d['t9'],-5.15)
    def test_no_kelvin_from_rgb(self):
        r=spectral(dict(pixel(unit='DN'),display_rgba=[0,0,255,255]));self.assertEqual(r['phase'],'unknown')
    def test_water_candidate_not_icing(self):
        r=spectral(pixel());self.assertEqual(r['phase'],'water_candidate');self.assertNotIn('icing',r)
    def test_ice_candidate(self):
        r=spectral(pixel({7:229,9:230,10:229}));self.assertEqual(r['phase'],'ice_candidate')
    def test_missing_phase_channel(self):
        r=spectral(pixel({9:250}));self.assertIn('phase_missing',[c['id'] for c in r['cards']])
    def test_bad_difference_suppresses_hypotheses(self):
        r=spectral(pixel({7:261,9:267,10:116}));self.assertFalse(r['quality_ok']);self.assertEqual(r['phase'],'unknown')
    def test_nan_not_an_observation(self):
        r=spectral(pixel({9:float('nan')}));self.assertEqual(r['metrics'],[])
    def test_night_only(self):
        a=spectral(pixel(sun=-10));b=spectral(pixel(sun=20))
        self.assertIn('night_water',a['conditions']);self.assertNotIn('night_water',b['conditions'])
    def test_cold_is_not_confirmed_cb(self):
        r=spectral(pixel({5:230,7:229,9:230,10:229}))
        c=next(c for c in r['cards'] if c['id']=='convection');self.assertIn('не установлены',c['meaning'])
    def test_reference_colours_are_recipe_specific(self):
        self.assertNotEqual(GUIDES['micro24']['swatches'],GUIDES['night']['swatches'])
        self.assertIn('не универсальный',GUIDES['micro24']['swatches'][-1]['meaning'])
    def test_vectorized_rules_match_point_core(self):
        arrays={7:np.array([[229,264,270,250,np.nan]]),9:np.array([[230,268,271,267,np.nan]]),10:np.array([[229,267,270,116,np.nan]])}
        out=phase_field(arrays,np.isfinite(arrays[9]));self.assertEqual(out.tolist(),[[1,2,4,5,0]])
        for i in (0,1,2,3):
            q=spectral(pixel({k:float(v[0,i]) for k,v in arrays.items()}))
            self.assertEqual({1:'ice_candidate',2:'water_candidate',4:'unknown',5:'unknown'}[out[0,i]],q['phase'])
    def test_uint16_vectorized_no_underflow(self):
        a={k:np.array([[v]],dtype=np.uint16) for k,v in {7:264,9:268,10:267}.items()}
        self.assertEqual(phase_field(a,np.ones((1,1),bool))[0,0],2)


class ProfileTests(unittest.TestCase):
    def test_units_c_hpa(self):
        p=profile(temperature_units='C',temperature=[6.85,.85,-5.15,-11.15],pressure_units='hPa',pressure=[1000,890,790,700])
        self.assertAlmostEqual(p['temperature'][0],280);self.assertEqual(p['pressure'][0],100000)
    def test_source_required(self):
        with self.assertRaises(ValueError):profile(source='')
    def test_timezone_required(self):
        with self.assertRaises(ValueError):profile(valid_time='2026-01-01T00:00:00')
    def test_bad_height(self):
        with self.assertRaises(ValueError):profile(height=[0,1000,1000,3000])
    def test_agl_rejected(self):
        with self.assertRaises(ValueError):profile(vertical_reference='AGL')
    def test_nonfinite_profile(self):
        with self.assertRaises(ValueError):profile(temperature=[280,np.nan,260,250])
    def test_incorrect_pressure_order(self):
        with self.assertRaises(ValueError):profile(pressure=[70000,79000,89000,100000])
    def test_condensate_units(self):
        with self.assertRaises(ValueError):profile(condensate_units='g/m3')
    def test_only_one_wind_component(self):
        with self.assertRaises(ValueError):profile(u=[0,0,0,0],wind_units='m/s')
    def test_csv(self):
        p=parse_profile(dict(text='height_m,temperature_c,pressure_hpa\n0,5,1000\n1000,-1,890',metadata=dict(source='TEST',valid_time='2026-01-01T00:00:00Z',lat=70,lon=30)))
        self.assertEqual(p['pressure'],[100000,89000]);self.assertAlmostEqual(p['temperature'][0],278.15)
    def test_csv_semicolon_decimal_comma(self):
        p=parse_profile(dict(text='height_m;temperature_c\n0;5,5\n1000;-1,5',metadata=dict(source='TEST',valid_time='2026-01-01T00:00:00Z',lat=70,lon=30)))
        self.assertAlmostEqual(p['temperature'][1],271.65)
    def test_csv_no_ambiguous_temperature_columns(self):
        with self.assertRaises(ValueError):parse_profile(dict(text='height_m,temperature_c,temperature_k\n0,5,280\n1000,-1,271',metadata=dict(source='T',lat=70,lon=30,valid_time='2026-01-01T00:00:00Z')))
    def test_outside_geography(self):
        r=applicability(profile(),-50,70,'2026-01-01T00:00:00Z');self.assertFalse(r['applicable'])
    def test_outside_time(self):
        r=applicability(profile(),30,70,'2026-01-01T06:00:00Z');self.assertFalse(r['applicable'])
    def test_no_cloud_confirmation_no_height(self):
        p=pixel();r=profile_diagnostics(profile(),p,spectral(p),2000);self.assertEqual(r['height']['status'],'needs_cloud')
    def test_no_opacity_confirmation_no_height(self):
        p=pixel();r=profile_diagnostics(profile(),p,spectral(p),2000,True,False);self.assertEqual(r['height']['status'],'needs_opacity')
    def test_height_roots_and_sensitivity(self):
        p=pixel();r=profile_diagnostics(profile(),p,spectral(p),2000,True,True,2)
        self.assertEqual(r['height']['candidate_heights_m'],[2000]);np.testing.assert_allclose(r['height']['sensitivity_ranges_m'],[[1666.6666667,2333.3333333]])
    def test_thin_layer_no_simple_height(self):
        p=pixel({7:264,9:268,10:264});r=profile_diagnostics(profile(),p,spectral(p),2000,True,True)
        self.assertEqual(r['height']['status'],'thin')
    def test_inversion_all_ranges(self):
        self.assertEqual(height_ranges([0,1000,2000],[280,270,280],275,1),[[400,600],[1400,1600]])
    def test_flat_range(self):self.assertEqual(height_ranges([0,1000],[270,270],270,1),[[0,1000]])
    def test_icing_requires_actual_profile_liquid(self):
        pp=profile();pp.pop('cloud_liquid');p=pixel()
        r=profile_diagnostics(pp,p,spectral(p),2000);self.assertIsNone(r['layer']['icing_conditions'])
        self.assertEqual(r['layer']['status'],'missing_water')
    def test_cold_liquid_has_potential_not_severity(self):
        p=pixel();r=profile_diagnostics(profile(),p,spectral(p),2000)
        self.assertTrue(r['layer']['icing_conditions']);self.assertNotIn('severity',r['layer'])
        self.assertGreater(r['layer']['liquid_water_g_m3'],.1)
    def test_ice_top_not_transferred_to_layer(self):
        p=pixel({7:229,9:230,10:229});pp=profile();pp.pop('cloud_liquid')
        r=profile_diagnostics(pp,p,spectral(p),2000);self.assertIsNone(r['layer']['icing_conditions'])
    def test_no_condensate_not_safe(self):
        p=pixel();r=profile_diagnostics(profile(cloud_liquid=[0]*4),p,spectral(p),2000)
        self.assertEqual(r['layer']['status'],'not_resolved');self.assertIsNone(r['layer']['icing_conditions'])
    def test_warm_air_is_not_a_no_icing_guarantee(self):
        p=pixel();r=profile_diagnostics(profile(),p,spectral(p),0)
        self.assertIsNone(r['layer']['icing_conditions'])
    def test_outside_height_no_extrapolation(self):
        p=pixel();r=profile_diagnostics(profile(),p,spectral(p),4000);self.assertEqual(r['layer']['status'],'outside_height')
    def test_outside_profile_blocks_height_and_layer(self):
        p=pixel(time='2026-01-03T00:00:00Z');r=profile_diagnostics(profile(),p,spectral(p),2000,True,True)
        self.assertEqual(r['layer']['status'],'outside');self.assertEqual(r['height']['status'],'outside')
    def test_column_provenance(self):
        p=pixel();r=profile_diagnostics(profile(),p,spectral(p),2000)
        self.assertEqual(r['column']['pressure_range_pa'],[70000,100000]);self.assertGreater(r['column']['vapor_mm'],0)


class AnalysisIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.scene=fixture(self.root);self.app=Workstation(self.root/'state');self.app.scan_local(self.root)
        self.product=build_product(self.scene,self.app.product_root,dict(product='micro24',preset='barents',width=256,channel=9),{'mode':'assumed'})
        x,y=project_points(self.app.product_grid(self.product),[(30,70)])[0]
        self.query=dict(product=self.product['id'],x=x,y=y)
    def tearDown(self):self.app.close();self.tmp.cleanup()
    def test_point_all_source_inputs_and_export(self):
        r=self.app.analyse(self.query);self.assertEqual(len(r['inputs']),7)
        self.assertEqual(json.loads(self.app.analysis_export(r['id']))['analysis']['phase'],r['analysis']['phase'])
    def test_profile_persisted_selected_by_id(self):
        p=self.app.import_profile(profile());r=self.app.analyse(dict(self.query,profile_id=p['id'],altitude_m=2000))
        self.assertEqual(len(self.app.profiles()),1);self.assertEqual(r['profile_id'],p['id'])
        self.assertEqual(r['profile_result']['layer']['status'],'supercooled')
    def test_view_settings_persist_and_validate(self):
        self.app.configure({'view_settings':{'preset':'barents','product':'phase','channel':9}})
        self.assertEqual(self.app.state()['view_settings']['product'],'phase')
        with self.assertRaises(ValueError):self.app.configure({'view_settings':{'preset':'unknown','product':'phase','channel':9}})
    def test_profile_id_traversal_rejected(self):
        with self.assertRaises(ValueError):self.app.get_profile('../../secret')
    def test_analysis_id_traversal_rejected(self):
        with self.assertRaises(ValueError):self.app.analysis_export('../../secret')
    def test_export_contains_explanatory_legend(self):
        self.assertIn('Красные тона',self.product['legend']['interpretation']['swatches'][0]['title'])
        self.assertIn('Красные тона',(self.app.product_root/self.product['id']/'report.html').read_text(encoding='utf-8'))
    def test_new_phase_product(self):
        p=build_product(self.scene,self.app.product_root,dict(product='phase',preset='barents',width=256),{'mode':'assumed'})
        self.assertEqual(len(p['legend']['classes']),6);self.assertIn('values.tif',p['files'])
    def test_route_retains_profile_eta_and_csv(self):
        p=self.app.import_profile(profile())
        r=self.app.route(dict(product=self.product['id'],points=[[30,70],[31,70]],step_km=25,speed_kmh=300,departure='2026-01-01T06:00:00Z',profile_id=p['id'],altitude_m=2000))
        self.assertFalse(r['samples'][0]['profile_result']['profile']['applicable'])
        self.assertIn('profile_liquid_water_g_m3',self.app.route_export(r['id'],'csv').decode('utf-8-sig'))
        self.assertEqual(len(r['inputs_all']),7)
