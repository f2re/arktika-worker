"""Синтетическое содержимое проверяет транспорт и контракт файлов, а не погоду."""
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from arktika.gptl_native_cli import capture
from arktika.network import Client


CHANNEL = "https://s3.gptl.ru/example/scene_ch9.tif?versionId=source-v1"
QUALITY = "https://s3.gptl.ru/example/quality.tif?versionId=quality-v1"
ITEM = "https://s3.gptl.ru/example/scene.json"
EXTENSION = "https://stac-extensions.github.io/timestamps/v1.1.0/schema.json"
LOCAL_TIMES = [datetime(2024, 6, 3, hour, tzinfo=timezone.utc) for hour in (1, 2)]


def geotiff(value, dtype, *, scale=1.0):
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=3, height=2, count=1, dtype=dtype,
                         crs="EPSG:4326", transform=from_origin(10, 60, 0.05, 0.05)) as dataset:
            dataset.write(np.full((2, 3), value, dtype=dtype), 1)
            dataset.scales = (scale,)
        return memory.read()


def stac(platform, channel, quality):
    return {
        "type": "Feature", "id": f"test-{platform}",
        "stac_extensions": [EXTENSION],
        "properties": {"platform": platform, "processing:level": "L2IR",
                       "datetime": "2024-06-01T12:10:00Z", "start_datetime": "2024-06-01T12:00:00Z",
                       "end_datetime": "2024-06-01T12:15:00Z", "ready_at": "2024-06-01T12:16:00Z"},
        "assets": {
            "ch9": {"href": CHANNEL, "file:size": len(channel), "file:checksum": "1220" + sha256(channel).hexdigest(),
                    "raster:bands": [{"unit": "K", "scale": 0.1, "offset": 0.0}], "calibration:version": "fixture-v2",
                    "published": "2024-06-01T12:20:00Z"},
            "quality": {"href": QUALITY, "file:size": len(quality), "file:checksum": sha256(quality).hexdigest(),
                        "roles": ["quality"], "classification:classes": [{"value": 0, "name": "good"}],
                        "published": "2024-06-01T12:25:00Z"},
        }
    }


class Reply(BytesIO):
    def __init__(self, payload, version):
        super().__init__(payload)
        self.status = 200
        self.mode = "public"
        self.headers = {"content-length": str(len(payload)), "x-amz-version-id": version,
                        "content-type": "image/tiff", "etag": '"fixture"'}


class MockClient:
    def __init__(self, payloads, item=None):
        self.payloads = payloads
        self.item = item
        self.requests = []

    def validate(self, url):
        return Client().validate(url)

    def object(self, uri, *, cancel, maximum):
        self.requests.append(uri)
        if uri != ITEM or self.item is None or len(self.item) > maximum:
            raise AssertionError("unexpected metadata request")
        return {"body": self.item}

    def open_object(self, uri, headers, cancel, region):
        self.requests.append(uri)
        return Reply(*self.payloads[uri])


class NativeCaptureTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "item.json"
        self.geometry = self.root / "geometry-source.json"
        self.geometry.write_text(json.dumps({"reference": "fixture", "view_zenith_deg": 22, "footprint_km": 4}),
                                 encoding="utf-8")
        self.data = geotiff(2750, "uint16")
        self.qc = geotiff(0, "uint8")

    def setup_case(self, platform, output, *, adjust=None, actual_qc=None, item_url=False, **options):
        obj = stac(platform, self.data, self.qc)
        if adjust:
            adjust(obj)
        raw = b"\n" + json.dumps(obj, ensure_ascii=False).encode() + b"\n"
        self.source.write_bytes(raw)
        client = MockClient({CHANNEL: (self.data, "source-v1"), QUALITY: (actual_qc or self.qc, "quality-v1")}, raw)
        with patch("arktika.download.now", side_effect=LOCAL_TIMES):
            report = capture(client, output=output, raster_key="ch9", quality_key="quality",
                             **({"item_url": ITEM} if item_url else {"item_file": self.source}), **options)
        return report, raw, client.requests

    def test_native_candidate_for_both_families_preserves_binary_source(self):
        for platform, schema in (("ARCM1", "arktika-m-native-l2ir-1"), ("EL3", "electro-l-native-l2ir-1")):
            with self.subTest(platform=platform):
                output = self.root / platform
                report, raw, requests = self.setup_case(platform, output, geometry_file=self.geometry,
                                                        historical=True, item_url=platform == "EL3")
                self.assertEqual(report["status"], "candidate")
                self.assertFalse(report["model_ready"])
                self.assertEqual(report["available_at"], "2024-06-01T12:25:00Z")
                self.assertEqual(report["local_downloaded_at"], {"raster": "2024-06-03T01:00:00Z",
                                                                 "quality": "2024-06-03T02:00:00Z"})
                self.assertEqual(requests, ([ITEM] if platform == "EL3" else []) + [CHANNEL, QUALITY])
                if os.name == "posix":
                    self.assertEqual(output.stat().st_mode & 0o077, 0)
                native = json.loads((output / "native.json").read_bytes())
                self.assertEqual(native["schema"], schema)
                self.assertEqual(native["timing"]["availability_basis"], "provider_asset_publication")
                self.assertEqual(native["calibration"]["revision"], "fixture-v2")
                self.assertEqual((output / native["files"]["producer_item"]["path"]).read_bytes(), raw)
                self.assertEqual(set(native["files"]), {"producer_item", "asset", "raster", "download",
                                                        "quality", "quality_download", "geometry"})
                for local in native["files"].values():
                    self.assertEqual(sha256((output / local["path"]).read_bytes()).hexdigest(), local["sha256"])
                receipt = json.loads((output / native["files"]["quality_download"]["path"]).read_bytes())
                self.assertEqual(receipt["asset_id"], native["quality"]["asset_id"])
                self.assertEqual(receipt["object_version"], "quality-v1")

    def test_capture_without_geometry_keeps_local_time_separate(self):
        output = self.root / "without-geometry"
        report, _, requests = self.setup_case("ARCM2", output)
        self.assertEqual(report["status"], "captured")
        self.assertEqual(report["available_at"], "2024-06-03T02:00:00Z")
        self.assertEqual(report["provider_ready_at"], "2024-06-01T12:16:00Z")
        self.assertEqual(requests, [CHANNEL, QUALITY])
        self.assertFalse((output / "native.json").exists())

    def test_incomplete_open_metadata_keeps_source_and_never_downloads_raster(self):
        output = self.root / "incomplete-source"
        report, raw, requests = self.setup_case("EL3", output, adjust=lambda obj: obj["assets"].pop("quality"))
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["phase"], "supplier_metadata")
        self.assertEqual((output / "producer-item.json").read_bytes(), raw)
        self.assertFalse((output / "native.json").exists())
        self.assertEqual(requests, [])

    def test_quality_digest_mismatch_never_writes_native_manifest(self):
        output = self.root / "changed-quality"
        changed_qc = self.qc[:-1] + bytes([self.qc[-1] ^ 1])
        report, _, _ = self.setup_case("EL3", output, geometry_file=self.geometry, actual_qc=changed_qc)
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["phase"], "quality")
        self.assertFalse((output / "native.json").exists())

    def test_stac_and_header_calibration_conflict_never_writes_native_manifest(self):
        self.data = geotiff(2750, "uint16", scale=0.2)
        output = self.root / "calibration-conflict"
        report, _, _ = self.setup_case("EL3", output, geometry_file=self.geometry)
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("калибровка GeoTIFF", report["reason"])
        self.assertFalse((output / "native.json").exists())


if __name__ == "__main__":
    unittest.main()
