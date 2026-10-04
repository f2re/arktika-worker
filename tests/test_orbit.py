"""Тесты расчёта орбиты и геометрии наблюдения Арктика-М (SGP4/TLE)."""
import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from arktika.orbit import parse_tle_text, fetch_tle, calculate_viewing_geometry, BASELINE_TLES

class TestOrbit(unittest.TestCase):
    def test_parse_tle_text(self):
        sample = """
1 47719U 21016A   26206.87236863 -.00000386  00000-0  00000-0 0  9995
2 47719  63.2598  51.9767 7301002 270.1217  14.4714  2.00607275 39564
"""
        pair = parse_tle_text(sample)
        self.assertIsNotNone(pair)
        self.assertEqual(len(pair), 2)
        self.assertTrue(pair[0].startswith('1 47719'))
        self.assertTrue(pair[1].startswith('2 47719'))

    def test_fetch_tle_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            pair, source = fetch_tle(47719, Path(tmp))
            self.assertEqual(len(pair), 2)
            self.assertIn('47719', pair[0])

    def test_calculate_viewing_geometry_arcm1(self):
        when = datetime(2026, 9, 28, 23, 45, tzinfo=timezone.utc)
        geom = calculate_viewing_geometry('ARCM1', when, 73.0, 15.0)
        self.assertEqual(geom['platform'], 'ARCM1')
        self.assertEqual(geom['norad_id'], 47719)
        self.assertGreater(geom['zenith_deg'], 40.0)
        self.assertLess(geom['zenith_deg'], 70.0)
        self.assertGreater(geom['distance_km'], 30000.0)
        self.assertGreater(geom['subpoint']['alt_km'], 30000.0)

    def test_calculate_viewing_geometry_unknown_platform(self):
        when = datetime(2026, 9, 28, 23, 45, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            calculate_viewing_geometry('UNKNOWN_SAT', when, 73.0, 15.0)

if __name__ == '__main__':
    unittest.main()
