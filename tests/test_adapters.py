"""Контракт локальных производных адаптеров; сетевые данные не запрашиваются."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import requests

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
        with self.assertRaisesRegex(ValueError, "не смог выполнить"):
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

    def test_public_spec_rejects_paths_secrets_and_unbounded_text(self):
        for title in ("token=PRIVATE", "client_secret=PRIVATE", "AWS_SECRET_ACCESS_KEY=PRIVATE",
                      "AWS_ACCESS_KEY_ID=PRIVATE", "/Users/person/private-file", "x" * 121):
            with self.subTest(title=title), self.assertRaises(ValueError):
                AdapterSpec("safe_id", "1.0.0", "safe_product", title, "pkg.method",
                            "experimental", "stored_value", "Поле", "1", ("input",)).validate()

    def test_plugin_runtime_exception_is_scrubbed(self):
        class BrokenAdapter:
            spec = AdapterSpec("broken_runtime", "1.0.0", "broken_product", "Сломанный метод",
                               "test.broken", "experimental", "stored_value", "Поле", "1", ("u", "v"))

            def derive(self, inputs):
                raise RuntimeError("/private/path PRIVATE_TOKEN")

        registry = AdapterRegistry()
        registry.register(BrokenAdapter())
        with self.assertRaisesRegex(ValueError, "не смог выполнить") as error:
            registry.run("broken_runtime", self.inputs)
        self.assertNotIn("PRIVATE_TOKEN", str(error.exception))
        self.assertNotIn("/private/path", str(error.exception))

    def test_installed_entry_points_load_trusted_adapter_objects(self):
        class PluginAdapter:
            spec = AdapterSpec("test_plugin", "1.0.0", "test_product", "Плагин теста",
                               "test.plugin", "experimental", "stored_value",
                               "Тестовое поле", "1", ("input",))

            def derive(self, inputs):
                return np.asarray(inputs["input"].values)

        entries = [SimpleNamespace(name="test_plugin", load=lambda: PluginAdapter)]
        with patch("importlib.metadata.entry_points", return_value=entries):
            warnings = self.registry.load_installed()
        self.assertEqual(warnings, ())
        self.assertEqual({item["id"] for item in self.registry.catalogue()},
                         {"wind_speed", "test_plugin"})

    def test_bad_installed_entry_points_are_isolated_and_secrets_scrubbed(self):
        entries = [SimpleNamespace(name="broken_plugin",
                                   load=lambda: (_ for _ in ()).throw(RuntimeError("PRIVATE_TOKEN")))]
        with patch("importlib.metadata.entry_points", return_value=entries):
            warnings = self.registry.load_installed()
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("PRIVATE_TOKEN", repr(warnings))
        self.assertIn("wind_speed", {item["id"] for item in self.registry.catalogue()})

    def test_installed_plugin_cannot_self_declare_implemented_maturity(self):
        class UnreviewedAdapter:
            spec = AdapterSpec("unreviewed", "1.0.0", "unreviewed_product", "Плагин",
                               "third_party.method", "implemented", "stored_value",
                               "Поле", "1", ("input",))

            def derive(self, inputs):
                return np.asarray(inputs["input"].values)

        entry = SimpleNamespace(name="unreviewed", load=lambda: UnreviewedAdapter())
        with patch("importlib.metadata.entry_points", return_value=[entry]):
            warnings = self.registry.load_installed()
        self.assertEqual(warnings[0]["status"], "not_loaded")
        self.assertNotIn("unreviewed", {item["id"] for item in self.registry.catalogue()})


class RemoteRetryClassificationTests(unittest.TestCase):
    def test_only_temporary_transport_failures_are_retryable(self):
        from arktika.reanalysis.worker import _retryable_network_error
        self.assertTrue(_retryable_network_error(requests.Timeout()))
        self.assertTrue(_retryable_network_error(requests.ConnectionError()))
        self.assertFalse(_retryable_network_error(requests.exceptions.SSLError()))
        for status in (408,425,429,500,502,503,504):
            response=requests.Response();response.status_code=status
            self.assertTrue(_retryable_network_error(requests.HTTPError(response=response)))
        for status in (400,401,403,404):
            response=requests.Response();response.status_code=status
            self.assertFalse(_retryable_network_error(requests.HTTPError(response=response)))


if __name__ == "__main__":
    unittest.main()
