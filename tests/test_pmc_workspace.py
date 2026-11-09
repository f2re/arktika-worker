"""Persistent operator workspaces: restart, provenance, HTTP and input boundaries."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from arktika.geo import grid
from arktika.pmc_workspace import SCHEMA_VERSION, validate_workspace
from arktika.workstation import Workstation
from server import LocalServer
from pyproj import Transformer
from unittest.mock import patch


def workspace():
    g = grid("barents", 512)
    lon, lat = Transformer.from_crs(g["crs"], "EPSG:4326", always_xy=True).transform(*(g["transform"] * (100, 100)))
    return {
        "schema_version": SCHEMA_VERSION,
        "map": {**{k: g[k] for k in ("preset", "width", "height", "crs", "bounds")},
                "view": [40, 30, 256, 256]},
        "product_id": None,
        "field_stack": [{"id": "a" * 24, "style": "arrows", "visible": True, "opacity": 0.65}],
        "raster_visible": True,
        "night_visible": False,
        "run": {"field_id": "a" * 24, "preset": "barents", "width": 512,
                "observation_id": None, "thresholds": {"minimum_vorticity_s-1": 1e-4, "peak_vorticity_s-1": 1.2e-4}},
        "archive": {"sources": ["era5"], "start": "2024-01-01T00:00:00Z",
                    "end": "2024-01-01T03:00:00Z", "area": [80, -20, 60, 70], "include_context": True},
        "result": {"field_id": "a" * 24, "valid_time": "2024-01-01T00:00:00Z",
                   "source": "era5", "maturity": "experimental", "candidate_count": 1,
                   "algorithm_version": "synthetic-test", "grid": {"crs": g["crs"]},
                   "candidates": [{"candidate_id": "c1", "classification": "candidate", "confirmation_status": "not_confirmed"}]},
        "annotations": [{"id": "an-test", "x": 100, "y": 100, "lon": lon, "lat": lat,
                         "label": "Ручная оценка", "note": "Синтетическая проверка программного состояния",
                         "status": "expert_supported", "candidate_id": "c1",
                         "valid_time": "2024-01-01T00:00:00Z", "source": "test"}],
    }


class PmcWorkspacePersistence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"
        self.app = Workstation(self.root)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_restart_restores_map_snapshot_marks_thresholds_and_reports_missing_fields(self):
        state = workspace()
        saved = self.app.pmc_bookmark_save({"title": "Баренцево море", "notes": "Вернуться к случаю", "state": state})["bookmark"]
        self.app.close()
        self.app = Workstation(self.root)
        restored = self.app.pmc_bookmark(saved["id"])["bookmark"]
        self.assertEqual(restored["state"], state)
        self.assertEqual(restored["status"], "saved_analysis_not_recomputed")
        self.assertEqual(restored["availability"]["missing_fields"], ["a" * 24])
        self.assertEqual(restored["state"]["result"]["candidates"][0]["confirmation_status"], "not_confirmed")
        self.assertEqual(len(self.app.pmc_bookmarks()["bookmarks"]), 1)
        self.assertNotIn("state", self.app.pmc_bookmarks()["bookmarks"][0])

    def test_update_keeps_identity_and_creation_time_then_delete(self):
        first = self.app.pmc_bookmark_save({"title": "Первый", "state": workspace()})["bookmark"]
        second = self.app.pmc_bookmark_save({"id": first["id"], "title": "Уточнённый", "notes": "Новая заметка", "state": workspace()})["bookmark"]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(second["title"], "Уточнённый")
        self.assertTrue(self.app.pmc_bookmark_delete({"id": first["id"]})["deleted"])
        self.assertFalse(self.app.pmc_bookmark_delete({"id": first["id"]})["deleted"])
        self.assertEqual(self.app.pmc_bookmarks()["bookmarks"], [])

    def test_checksum_detects_changed_saved_result(self):
        saved = self.app.pmc_bookmark_save({"title": "Тест целостности", "state": workspace()})["bookmark"]
        with self.app.store.lock, self.app.store.conn:
            self.app.store.conn.execute("UPDATE pmc_bookmarks SET state=? WHERE id=?", ("{}", saved["id"]))
        with self.assertRaisesRegex(ValueError, "контрольная сумма"):
            self.app.pmc_bookmark(saved["id"])

    def test_snapshot_checks_both_input_ids_and_original_file_digest(self):
        state = workspace()
        state["run"]["field_id"] = "b" * 24
        state["result"]["field_passport"] = {"claimed_source_file_sha256": "1" * 64}
        saved = self.app.pmc_bookmark_save({"title": "Проверка исходников", "state": state})["bookmark"]
        with patch.object(self.app, "field", return_value={"sha256": "2" * 64}) as read:
            loaded = self.app.pmc_bookmark(saved["id"])["bookmark"]
        self.assertEqual({call.args[0] for call in read.call_args_list}, {"a" * 24, "b" * 24})
        self.assertEqual(loaded["availability"]["changed_fields"], ["a" * 24])
        self.assertIn("client_supplied", loaded["snapshot_attestation"])
        self.assertTrue(any("другой исходный" in warning for warning in loaded["availability"]["warnings"]))

    def test_empty_workspace_is_valid_without_calculation_or_cached_data(self):
        state = workspace()
        state.update(result=None, run=None, archive=None, field_stack=[], annotations=[])
        result = self.app.pmc_bookmark_save({"title": "Подготовка", "state": state})["bookmark"]
        self.assertIsNone(result["state"]["result"])

    def test_expert_note_does_not_promote_detector_or_accept_unexplained_claim(self):
        state = workspace()
        state["annotations"][0]["note"] = ""
        with self.assertRaisesRegex(ValueError, "наблюдаемые признаки"):
            validate_workspace(state)
        state = workspace()
        state["result"]["candidates"][0]["confirmation_status"] = "confirmed"
        with self.assertRaisesRegex(ValueError, "отдельно"):
            validate_workspace(state)

    def test_rejects_secrets_nonfinite_wrong_time_and_unknown_fields(self):
        invalid = []
        state = workspace(); state["result"]["access_token"] = "DO_NOT_STORE"; invalid.append(state)
        state = workspace(); state["map"]["view"][0] = float("nan"); invalid.append(state)
        state = workspace(); state["annotations"][0]["valid_time"] = "2024-01-01T00:00:00"; invalid.append(state)
        state = workspace(); state["annotations"][0]["lat"] += 1; invalid.append(state)
        state = workspace(); state["field_stack"][0]["id"] = "../../secret"; invalid.append(state)
        state = workspace(); state["annotations"] = state["annotations"] * 251; invalid.append(state)
        state = workspace(); state["untrusted_html"] = "<script>"; invalid.append(state)
        state = workspace(); state["result"]["field_id"] = []; invalid.append(state)
        state = workspace(); state["result"]["field_passport"] = "not an object"; invalid.append(state)
        state = workspace(); state["result"]["parameters"] = [1, 2]; invalid.append(state)
        state = workspace(); state["result"]["candidate_count"] = True; invalid.append(state)
        for state in invalid:
            with self.subTest(state_keys=list(state)), self.assertRaises(ValueError):
                self.app.pmc_bookmark_save({"title": "Некорректный", "state": state})
        self.assertEqual(self.app.pmc_bookmarks()["bookmarks"], [])

    def test_invalid_update_does_not_overwrite_saved_state(self):
        state = workspace()
        first = self.app.pmc_bookmark_save({"title": "Сохранить", "state": state})["bookmark"]
        invalid = copy.deepcopy(state)
        invalid["map"]["bounds"] = [0, 0, 0, 0]
        with self.assertRaises(ValueError):
            self.app.pmc_bookmark_save({"id": first["id"], "title": "Неверный", "state": invalid})
        restored = self.app.pmc_bookmark(first["id"])["bookmark"]
        self.assertEqual(restored["title"], "Сохранить")
        self.assertEqual(restored["state_sha256"], first["state_sha256"])


class PmcWorkspaceHTTP(unittest.TestCase):
    def test_authenticated_roundtrip_and_csrf_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Workstation(Path(temp) / "state")
            server = LocalServer(("127.0.0.1", 0), app, key="workspace-test")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def request(path, payload=None, headers=None):
                base_headers = {"Cookie": "arktika_session=workspace-test", "Content-Type": "application/json", "X-Arktika-Request": "1"}
                base_headers.update(headers or {})
                req = urllib.request.Request(base + path, data=json.dumps(payload).encode() if payload is not None else None, headers=base_headers)
                with opener.open(req, timeout=5) as response:
                    return json.load(response)

            try:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request("/api/pmc/bookmarks", headers={"Cookie": ""})
                self.assertEqual(error.exception.code, 401)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request("/api/pmc/bookmarks/save", {"title": "x", "state": workspace()}, {"Origin": "https://example.org"})
                self.assertEqual(error.exception.code, 403)
                saved = request("/api/pmc/bookmarks/save", {"title": "HTTP тест", "state": workspace()})["bookmark"]
                restored = request("/api/pmc/bookmark?id=" + saved["id"])["bookmark"]
                self.assertEqual(restored["state"], workspace())
                self.assertEqual(len(request("/api/pmc/bookmarks")["bookmarks"]), 1)
                self.assertTrue(request("/api/pmc/bookmarks/delete", {"id": saved["id"]})["deleted"])
            finally:
                server.shutdown(); thread.join(3); server.server_close(); app.close()


if __name__ == "__main__":
    unittest.main()
