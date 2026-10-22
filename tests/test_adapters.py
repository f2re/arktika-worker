"""Контракт локальных производных адаптеров; сетевые данные не запрашиваются."""
import unittest
from dataclasses import replace

import numpy as np

from arktika.adapters import (
    AdapterRegistry,
    AdapterSpec,
    SourceData,
    WindSpeedAdapter,
)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.registry = AdapterRegistry()
        self.registry.register(WindSpeedAdapter())
        self.grid = {
            "crs": "EPSG:3413",
            "width": 3,
            "height": 2,
            "transform": [1000, 0, 0, 0, -1000, 2000],
        }
        self.inputs = {
            "u": SourceData(
                "u", np.array([[3.0, 0.0, np.nan], [1.0, -4.0, 0.0]]),
                "eastward_wind", "m s-1", "2026-01-01T00:00:00Z", self.grid,
                {"id": "ERA5", "dataset": "pressure-levels", "sha256": "a" * 64},
            ),
            "v": SourceData(
                "v", np.array([[4.0, 2.0, 1.0], [0.0, 3.0, 0.0]]),
                "northward_wind", "m s-1", "2026-01-01T00:00:00+00:00", self.grid,
                {"id": "ERA5", "dataset": "pressure-levels", "sha256": "b" * 64},
            ),
        }

    def test_registered_example_returns_versioned_product_and_passport(self):
        result = self.registry.run("wind_speed", self.inputs)
        np.testing.assert_allclose(result.values[0, :2], [5.0, 2.0])
        self.assertTrue(np.isnan(result.values[0, 2]))
        self.assertFalse(result.values.flags.writeable)
        self.assertEqual(result.spec.version, "1.0.0")
        self.assertEqual(result.passport["algorithm"],
                         {"id": "arktika.wind_speed", "version": "1.0.0"})
        self.assertEqual(result.passport["valid_time"], "2026-01-01T00:00:00Z")
        self.assertEqual(result.passport["uncertainty"]["status"], "not_assessed")
        self.assertEqual(len(result.sources), 2)

    def test_catalogue_exposes_metadata_only(self):
        row = self.registry.catalogue()[0]
        self.assertEqual(row["id"], "wind_speed")
        self.assertNotIn("derive", row)
        row["id"] = "changed"
        self.assertEqual(self.registry.catalogue()[0]["id"], "wind_speed")

    def test_unknown_or_duplicate_adapter_rejected(self):
        with self.assertRaisesRegex(ValueError, "Неизвестный адаптер"):
            self.registry.get("../user-code")
        with self.assertRaisesRegex(ValueError, "уже зарегистрирован"):
            self.registry.register(WindSpeedAdapter())

    def test_inputs_must_match_contract_and_grid(self):
        with self.assertRaisesRegex(ValueError, "контрактом адаптера"):
            self.registry.run("wind_speed", {"u": self.inputs["u"]})
        bad = dict(self.inputs)
        bad["v"] = replace(self.inputs["v"], grid={
            **self.grid, "transform": [1000, 0, 10, 0, -1000, 2000]
        })
        with self.assertRaisesRegex(ValueError, "точно совпадающие сетки"):
            self.registry.run("wind_speed", bad)

    def test_inputs_must_have_matching_utc_time(self):
        bad = dict(self.inputs)
        item = self.inputs["v"]
        bad["v"] = SourceData(item.input_id, item.values, item.quantity, item.units,
                              "2026-01-01T03:00:00Z", item.grid, item.source)
        with self.assertRaisesRegex(ValueError, "один точный срок UTC"):
            self.registry.run("wind_speed", bad)

    def test_rejects_wrong_wind_units_and_secret_provenance(self):
        bad = dict(self.inputs)
        item = self.inputs["u"]
        bad["u"] = SourceData(item.input_id, item.values, item.quantity, "knots",
                              item.valid_time, item.grid, item.source)
        with self.assertRaisesRegex(ValueError, "m s-1"):
            self.registry.run("wind_speed", bad)
        bad = dict(self.inputs)
        item = self.inputs["u"]
        bad["u"] = SourceData(item.input_id, item.values, item.quantity, item.units,
                              item.valid_time, item.grid, {"id": "ERA5", "token": "secret"})
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.registry.run("wind_speed", bad)

    def test_spec_requires_explicit_semver_and_inputs(self):
        class InvalidAdapter:
            spec = AdapterSpec(
                "my_method", "latest", "my_product", "Название", "arktika.my_method",
                "experimental", "stored_value", "Значение", "1", ("input",),
            )

            def derive(self, inputs):
                return np.zeros((1, 1))

        with self.assertRaisesRegex(ValueError, "major.minor.patch"):
            self.registry.register(InvalidAdapter())


if __name__ == "__main__":
    unittest.main()
