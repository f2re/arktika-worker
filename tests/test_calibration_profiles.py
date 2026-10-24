"""Тесты профилей калибровки МСУ-ГС/А по данным реанализа ERA5/RTTOV.

Проверяются:
- Валидация JSON-схем калибровки для КА «Арктика-М» №1 и №2.
- Запрет перевода видимых каналов 1–3 в Кельвины.
- Строгая изоляция платформ (ARCM1 vs ARCM2).
- Корректность применения калибровки в product_flow и паспортах продуктов.
"""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest

import rasterio

from arktika.processing import (
    CalibrationError,
    build_product,
    reanalysis_calibration_profile,
    validate_calibration,
)
from arktika.product_schema import validate_product_passport
from arktika.workstation import Workstation

ROOT = Path(__file__).resolve().parent.parent


class TestCalibrationProfilesValidation(unittest.TestCase):
    def test_arcm1_profile_valid(self):
        cfg_path = ROOT / "config" / "calibration.arcm1.json"
        self.assertTrue(cfg_path.is_file(), "config/calibration.arcm1.json должен существовать")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        validated = validate_calibration(cfg)
        self.assertEqual(validated["mode"], "declared")
        self.assertEqual(validated["platform"], "ARCM1")
        self.assertIn("reference", validated)
        self.assertTrue(len(validated["reference"].strip()) > 0)

        # Проверка каналов 4-10
        channels = validated["channels"]
        for ch in ("4", "5", "6", "7", "8", "9", "10"):
            self.assertIn(ch, channels, f"Канал {ch} должен быть объявлен")
            c = channels[ch]
            self.assertEqual(c.get("units"), "K")
            self.assertGreater(c.get("scale"), 0)
            self.assertTrue(math.isfinite(c.get("offset")))

        # Каналы 1-3 строго запрещены в Кельвинах
        for ch in ("1", "2", "3"):
            self.assertNotIn(ch, channels, f"Видимый канал {ch} не должен быть в ИК-калибровке")

    def test_arcm2_profile_valid(self):
        cfg_path = ROOT / "config" / "calibration.arcm2.json"
        self.assertTrue(cfg_path.is_file(), "config/calibration.arcm2.json должен существовать")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        validated = validate_calibration(cfg)
        self.assertEqual(validated["mode"], "declared")
        self.assertEqual(validated["platform"], "ARCM2")
        self.assertIn("reference", validated)

        channels = validated["channels"]
        for ch in ("4", "5", "6", "7", "8", "9", "10"):
            self.assertIn(ch, channels)
            c = channels[ch]
            self.assertEqual(c.get("units"), "K")
            self.assertGreater(c.get("scale"), 0)
            self.assertTrue(math.isfinite(c.get("offset")))

        for ch in ("1", "2", "3"):
            self.assertNotIn(ch, channels)

    def test_example_profile_valid(self):
        cfg_path = ROOT / "config" / "calibration.example.json"
        self.assertTrue(cfg_path.is_file())
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        validated = validate_calibration(cfg)
        self.assertEqual(validated["mode"], "declared")

    def test_processing_helper_loads_profiles(self):
        p1 = reanalysis_calibration_profile("ARCM1")
        self.assertEqual(p1["platform"], "ARCM1")
        self.assertEqual(p1["mode"], "declared")
        self.assertAlmostEqual(p1["channels"]["9"]["scale"], 0.3347417, places=5)

        p2 = reanalysis_calibration_profile("ARCM2")
        self.assertEqual(p2["platform"], "ARCM2")
        self.assertEqual(p2["mode"], "declared")
        self.assertAlmostEqual(p2["channels"]["9"]["scale"], 0.3347417, places=5)

        with self.assertRaises(CalibrationError):
            reanalysis_calibration_profile("UNKNOWN")

    def test_visible_channels_strictly_rejected(self):
        cfg = copy.deepcopy(reanalysis_calibration_profile("ARCM1"))
        for bad_ch in ("1", "2", "3"):
            cfg_bad = copy.deepcopy(cfg)
            cfg_bad["channels"][bad_ch] = {"scale": 1.0, "offset": 0.0, "units": "K"}
            with self.assertRaises(CalibrationError):
                validate_calibration(cfg_bad)

    def test_extra_top_level_keys_rejected(self):
        cfg = copy.deepcopy(reanalysis_calibration_profile("ARCM1"))
        cfg["extra_key"] = "forbidden"
        with self.assertRaises(CalibrationError):
            validate_calibration(cfg)


class TestCalibrationPlatformIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Создаём синтетическую сцену ARCM1 и ARCM2
        from test_science import fixture
        self.scene_arcm2 = fixture(self.root)
        self.scene_arcm2["platform"] = "ARCM2"
        self.scene_arcm1 = copy.deepcopy(self.scene_arcm2)
        self.scene_arcm1["id"] = "ARCM1_20260928234500"
        self.scene_arcm1["platform"] = "ARCM1"

        self.app = Workstation(self.root / "state", self.root / "downloads")

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_workstation_set_calibration_requires_platform(self):
        cfg = {"mode": "declared", "reference": "Ref", "channels": {"9": {"scale": 1.0, "offset": 0.0, "units": "K"}}}
        with self.assertRaises(ValueError):
            self.app.set_calibration(cfg)

    def test_workstation_set_calibration_persists_valid_profile(self):
        cfg1 = reanalysis_calibration_profile("ARCM1")
        res = self.app.set_calibration(cfg1)
        self.assertEqual(res["platform"], "ARCM1")
        self.assertEqual(self.app.cal["platform"], "ARCM1")

    def test_platform_mismatch_prevents_global_calibration(self):
        # Настраиваем калибровку для ARCM1
        self.app.set_calibration(reanalysis_calibration_profile("ARCM1"))

        # Проверяем для сцены ARCM2: глобальная калибровка не должна примениться
        cal_config, issues = self.app.radiometry_config(self.scene_arcm2)
        for ch in ("4", "7", "9", "10"):
            self.assertIn(ch, issues)
            self.assertEqual(issues[ch], "Глобальная калибровка задана для другого аппарата.")

        # А для сцены ARCM1 калибровка должна успешно примениться без ошибок
        cal_config1, issues1 = self.app.radiometry_config(self.scene_arcm1)
        for ch in ("4", "7", "9", "10"):
            self.assertNotIn(ch, issues1)
            self.assertIn(ch, cal_config1["channels"])
            self.assertEqual(cal_config1["channels"][ch]["status"], "declared")
            self.assertEqual(cal_config1["channels"][ch]["applicability_platform"], "ARCM1")


class TestProductGenerationWithCalibration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        from test_science import fixture
        self.scene = fixture(self.root)
        self.scene["platform"] = "ARCM1"

    def tearDown(self):
        self.tmp.cleanup()

    def test_declared_calibration_product_passport(self):
        cal = reanalysis_calibration_profile("ARCM1")
        target_dir = self.root / "products"

        product = build_product(
            self.scene,
            target_dir,
            {"product": "micro24", "preset": "barents", "width": 256},
            cal,
        )

        self.assertEqual(product["calibration_status"], "declared")
        passport = product.get("passport")
        self.assertIsNotNone(passport, "Паспорт продукта должен быть сформирован")

        # Валидация паспорта через product_schema
        validate_product_passport(passport)

        cal_meta = passport["calibration"]
        self.assertEqual(cal_meta["status"], "declared")
        self.assertEqual(cal_meta["version"], "calibration-config-v1")

        channels_prov = cal_meta.get("channels", [])
        self.assertTrue(len(channels_prov) > 0)
        for prov in channels_prov:
            self.assertEqual(prov["status"], "declared")
            self.assertEqual(prov["units"], "K")
            self.assertEqual(prov["applicability_platform"], "ARCM1")
            self.assertGreater(prov["scale"], 0)
            self.assertTrue(math.isfinite(prov["offset"]))


if __name__ == "__main__":
    unittest.main()
