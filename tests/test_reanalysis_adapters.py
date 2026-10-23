"""Software-only checks for the immutable source adapter registry."""
import dataclasses
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from arktika.reanalysis.adapters import (
    ADAPTERS,
    AdapterImplementation,
    AdapterMetadata,
    AdapterRegistry,
    get_adapter,
    get_runtime_adapter,
    register_adapter,
    require_supported,
    supports_variable,
    unregister_adapter,
)


class ReanalysisAdapterRegistryTests(unittest.TestCase):
    def test_registers_current_sources_with_provider_metadata(self):
        self.assertEqual(set(ADAPTERS), {"era5", "carra2", "merra2"})
        self.assertEqual(get_adapter("era5").transport_id, "cds")
        self.assertEqual(get_adapter("carra2").auth_provider, "cds")
        merra = get_adapter("merra2")
        self.assertEqual(merra.transport_id, "earthdata_cmr_opendap")
        self.assertEqual(merra.auth_provider, "earthdata")
        self.assertEqual(set(merra.dataset_ids), {"M2I3NPASM", "M2I1NXASM"})

    def test_supported_fields_match_existing_catalog_capabilities(self):
        self.assertTrue(supports_variable("era5", "omega"))
        self.assertFalse(supports_variable("carra2", "omega"))
        self.assertFalse(supports_variable("merra2", "rh"))
        self.assertTrue(supports_variable("carra2", "wind"))
        self.assertFalse(supports_variable("invented-source", "t"))
        self.assertFalse(supports_variable("era5", "invented-field"))

    def test_unknown_source_has_safe_error(self):
        with self.assertRaisesRegex(ValueError, "Неизвестный источник реанализа"):
            get_adapter("secret-or-unknown")

    def test_unsupported_field_has_safe_error(self):
        with self.assertRaisesRegex(ValueError, "не поддерживается источником"):
            require_supported("carra2", "omega")

    def test_metadata_and_registry_are_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            get_adapter("era5").name = "renamed"
        with self.assertRaises(TypeError):
            ADAPTERS["new-source"] = AdapterMetadata(
                source_id="new-source",
                name="unverified",
                transport_id="none",
                auth_provider="none",
                dataset_ids=(),
                supported_variables=frozenset(),
            )
        with self.assertRaises(AttributeError):
            get_adapter("era5").supported_variables.add("invented")

    def test_adapter_metadata_contains_no_credentials(self):
        for adapter in ADAPTERS.values():
            self.assertFalse(hasattr(adapter, "token"))
            self.assertFalse(hasattr(adapter, "password"))

    def test_builtin_adapters_are_executable_and_plan_through_registry(self):
        for source in ("era5", "carra2", "merra2"):
            adapter = get_runtime_adapter(source)
            self.assertTrue(callable(adapter.plan))
            self.assertTrue(callable(adapter.fetch))
            self.assertTrue(callable(adapter.normalize))
        plan = get_runtime_adapter("era5").plan({
            "source": "era5", "variable": "wind", "time": "2024-01-01T00:00:00Z",
            "level": 850, "area": [80, -20, 60, 20],
        })
        self.assertEqual(plan["source"], "era5")
        self.assertEqual(plan["variable"], "wind")

    def test_registered_adapter_runs_through_worker_and_writes_safe_cache(self):
        try:
            import xarray as xr

            from arktika.reanalysis.worker import prepare
        except ImportError:
            self.skipTest("Optional field dependencies unavailable")

        timestamp = "2024-01-01T00:00:00Z"
        plan = {
            "source": "synthetic", "variable": "wind", "time": timestamp,
            "level": 850.0, "area": [80.0, -20.0, 60.0, 20.0],
            "time_kind": "instantaneous_analysis", "resolution_km": 1.0,
        }
        canonical = xr.Dataset(
            {"value": (("y", "x"), np.ones((2, 3), dtype=np.float32)),
             "latitude": (("y", "x"), np.full((2, 3), 70.0)),
             "longitude": (("y", "x"), np.tile([0.0, 1.0, 2.0], (2, 1)))},
            coords={"time": np.datetime64("2024-01-01T00:00:00"),
                    "pressure_level": 850.0},
            attrs={"source": "synthetic", "variable": "wind", "unit": "m s-1",
                   "time": timestamp, "level": "850.0", "time_kind": "instantaneous_analysis",
                   "geolocation_crs": "EPSG:4326",
                   "resolution_km": 1.0, "area": json.dumps(plan["area"]),
                   "version": "synthetic-adapter-1"},
        )
        calls = []

        def planner(request):
            self.assertEqual(request, {"source": "synthetic", "variable": "wind"})
            return dict(plan)

        def fetcher(actual_plan, credential, folder, progress):
            self.assertEqual(actual_plan, plan)
            self.assertEqual(credential, {"token": "test-only-secret"})
            calls.append("fetch")
            progress("Synthetic adapter fetched a software fixture")
            return [canonical.copy(deep=True)], [{"kind": "synthetic_fixture", "sha256": "a" * 64}]

        def normalizer(dataset, actual_plan, area=None):
            self.assertEqual(actual_plan["source"], "synthetic")
            calls.append("normalize")
            return dataset

        implementation = AdapterImplementation(
            metadata=AdapterMetadata("synthetic", "Synthetic fixture", "fixture", "none",
                                     ("fixture-v1",), frozenset({"wind"})),
            planner=planner, fetcher=fetcher, normalizer=normalizer,
            catalogue_builder=lambda: {
                "id": "synthetic", "name": "Synthetic fixture", "auth": "none",
                "start": 2000, "step_hours": 1, "resolution_km": 1.0,
                "variables": [{"id": "wind", "title": "Ветер", "unit": "m s-1",
                               "vertical": "pressure", "levels": [850], "styles": ["fill"]}],
            },
        )
        register_adapter(implementation)
        try:
            from arktika.reanalysis.catalog import catalogue, request_plan
            registered_plan = request_plan({"source": "synthetic", "variable": "wind"})
            self.assertEqual(registered_plan, plan)
            self.assertIn("synthetic", {item["id"] for item in catalogue()["sources"]})

            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                result = prepare({
                    "plan": registered_plan, "cache": str(root / "fields"),
                    "identity": "synthetic-field-0000000001", "credential": {"token": "test-only-secret"},
                }, lambda _message: None)
                metadata = result["field"]
                metadata_text = (root / "fields" / "synthetic-field-0000000001" / "metadata.json").read_text(encoding="utf-8")
                self.assertEqual(calls, ["fetch", "normalize"])
                self.assertTrue((root / "fields" / "synthetic-field-0000000001" / "field.nc").is_file())
                self.assertEqual(metadata["source"], "synthetic")
                self.assertEqual(metadata["provenance"][0]["kind"], "synthetic_fixture")
                self.assertEqual(metadata["passport"]["schema_version"], "1.0")
                self.assertEqual(metadata["passport"]["grid"]["crs"], "EPSG:4326")
                self.assertEqual(metadata["passport"]["grid"]["width"], 3)
                self.assertEqual(metadata["passport"]["algorithm"], {"id": "synthetic", "version": "1.0.0"})
                self.assertNotIn("test-only-secret", metadata_text)
        finally:
            unregister_adapter("synthetic")

    def test_adapter_rejects_cross_source_plan_and_duplicate_registration(self):
        metadata = AdapterMetadata("isolated", "isolated", "none", "none", (), frozenset())
        adapter = AdapterImplementation(metadata, lambda _: {"source": "wrong"},
                                        lambda *_: ([], []), lambda value, *_: value,
                                        lambda: {"id":"isolated","variables":[]})
        registry = AdapterRegistry((adapter,))
        with self.assertRaisesRegex(ValueError, "другого источника"):
            registry.get("isolated").plan({})
        with self.assertRaisesRegex(ValueError, "уже зарегистрирован"):
            registry.register(adapter)

    def test_plan_rejects_non_http_private_uris_and_secret_keys(self):
        adapter = get_runtime_adapter("era5")
        plan = adapter.plan({"source":"era5","variable":"t","time":"2024-01-01T00:00:00Z",
                             "level":850,"area":[80,-20,60,20]})
        for extra in ({"archive":"s3://private-bucket/data"}, {"local":"file:///private/file.nc"},
                      {"api_key":"private"}, {"description":"source s3://private-bucket"}):
            unsafe = dict(plan, **extra)
            with self.assertRaisesRegex(ValueError, "точный срок UTC"):
                adapter._validate_plan(unsafe)
        for resolution in (True, math.inf, "Infinity"):
            with self.subTest(resolution=resolution), self.assertRaisesRegex(ValueError, "точный срок UTC"):
                adapter._validate_plan(dict(plan, resolution_km=resolution))

    def test_adapter_rejects_noncanonical_field_and_scrubs_provenance(self):
        import xarray as xr

        from arktika.reanalysis.worker import _safe_provenance
        plan = {"source": "isolated", "variable": "wind", "time": "2024-01-01T00:00:00Z",
                "level": 850.0, "area": [80.0, -20.0, 60.0, 20.0],
                "time_kind": "instantaneous_analysis", "resolution_km": 1.0}
        canonical = xr.Dataset(
            {"value": (("y", "x"), np.ones((2, 2))),
             "latitude": (("y", "x"), np.full((2, 2), 70.0)),
             "longitude": (("y", "x"), np.full((2, 2), 1.0))},
            coords={"time": np.datetime64("2024-01-01T00:00:00"), "pressure_level": 850.0},
            attrs={"source": "isolated", "variable": "wind", "unit": "m s-1",
                   "time": plan["time"], "level": "850.0", "time_kind": plan["time_kind"],
                   "resolution_km": 1.0, "geolocation_crs": "EPSG:4326",
                   "area": json.dumps(plan["area"])})
        implementation = AdapterImplementation(
            AdapterMetadata("isolated", "isolated", "fixture", "none", (), frozenset({"wind"})),
            lambda _: plan, lambda *_: ([], []), lambda value, *_: value,
            lambda: {"id": "isolated", "name": "isolated", "auth": "none", "start": 2000,
                     "step_hours": 1, "resolution_km": 1.0,
                     "variables": [{"id": "wind", "title": "Ветер", "unit": "m s-1",
                                    "vertical": "pressure", "levels": [850], "styles": ["fill"]}]})
        implementation.validate_canonical(canonical, plan)
        wrong = canonical.copy(deep=True)
        wrong.attrs["time"] = "2024-01-01T03:00:00Z"
        with self.assertRaisesRegex(ValueError, "не совпадает"):
            implementation.validate_canonical(wrong, plan)
        cleaned = _safe_provenance([{"dataset": "fixture", "url": "https://example.test/?token=secret",
                                     "token": "secret", "granule_id": "G1"}])
        encoded = json.dumps(cleaned)
        self.assertIn("G1", encoded)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("https://", encoded)

    def test_installed_adapter_entrypoint_is_loaded_by_worker_subprocess(self):
        source = '''
import numpy as np
import xarray as xr
from arktika.reanalysis.adapters import AdapterImplementation, AdapterMetadata

PLAN = {"source":"worker_fixture", "variable":"wind", "time":"2024-01-01T00:00:00Z",
        "level":850.0, "area":[80.0,-20.0,60.0,20.0],
        "time_kind":"instantaneous_analysis", "resolution_km":1.0}
def fetch(plan, credential, folder, progress):
    ds=xr.Dataset({"value":(("y","x"),np.ones((2,2))),
      "latitude":(("y","x"),np.full((2,2),70.0)),
      "longitude":(("y","x"),np.full((2,2),1.0))},
      coords={"time":np.datetime64("2024-01-01T00:00:00"),"pressure_level":850.0},
      attrs={"source":"worker_fixture","variable":"wind","unit":"m s-1",
       "time":PLAN["time"],"level":"850.0","time_kind":PLAN["time_kind"],
       "resolution_km":1.0,"geolocation_crs":"EPSG:4326","area":"[80.0, -20.0, 60.0, 20.0]"})
    return [ds],[{"dataset":"fixture","url":"https://secret.example/?token=hidden","token":"hidden"}]
def build():
    return AdapterImplementation(AdapterMetadata("worker_fixture","Fixture","fixture","none",
      ("fixture",),frozenset({"wind"})),lambda request:dict(PLAN),fetch,lambda ds,plan,area=None:ds,
      lambda:{"id":"worker_fixture","name":"Fixture","auth":"none","start":2000,
       "step_hours":1,"resolution_km":1.0,"variables":[{"id":"wind","title":"Ветер",
       "unit":"m s-1","vertical":"pressure","levels":[850],"styles":["fill"]}]})
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fixture_adapter.py").write_text(source, encoding="utf-8")
            dist = root / "fixture_adapter-1.0.dist-info"
            dist.mkdir()
            (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: fixture-adapter\nVersion: 1.0\n", encoding="utf-8")
            (dist / "entry_points.txt").write_text(
                "[arktika.reanalysis_adapters]\nfixture = fixture_adapter:build\n", encoding="utf-8")
            cache = root / "cache"
            payload = {"action":"prepare","plan":{"source":"worker_fixture","variable":"wind",
                "time":"2024-01-01T00:00:00Z","level":850.0,"area":[80.0,-20.0,60.0,20.0],
                "time_kind":"instantaneous_analysis","resolution_km":1.0},
                "credential":{"token":"private"},"identity":"fixture-field-0000000001","cache":str(cache),
                "work":str(root / "work")}
            env = dict(os.environ, PYTHONPATH=str(root) + os.pathsep + str(Path(__file__).resolve().parents[1]))
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/reanalysis_worker.py")],
                input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            metadata_path = cache / payload["identity"] / "metadata.json"
            self.assertTrue(metadata_path.is_file())
            saved = metadata_path.read_text(encoding="utf-8")
            self.assertIn('"title": "Ветер"', saved)
            self.assertNotIn("hidden", saved)
            self.assertNotIn("secret.example", saved)


if __name__ == "__main__":
    unittest.main()
