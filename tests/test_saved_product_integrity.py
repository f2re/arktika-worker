"""Byte-level and passport consistency tests for ordinary saved products."""
import copy
import tempfile
import unittest
from pathlib import Path

from arktika.product_schema import product_passport
from arktika.saved_product_integrity import (
    artifact_manifest,
    load_cached_product,
    product_metadata_path,
    replace_product_cache,
    validate_saved_product,
)


class SavedProductIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / ("a" * 24)
        self.folder.mkdir()
        (self.folder / "map.png").write_bytes(b"synthetic image")
        self.record = self._record()

    def tearDown(self):
        self.temp.cleanup()

    def _record(self):
        identity = self.folder.name
        transform = [1, 0, 0, 0, -1, 1]
        bounds = [0, 0, 1, 1]
        grid = {"crs": "EPSG:3413", "width": 1, "height": 1, "bounds": bounds,
                "transform": transform, "resolution": [1, -1], "resolution_units": "m"}
        passport = product_passport(
            product_id="channel", algorithm_id="arktika.channel", algorithm_version="test-1",
            maturity="implemented", quantity_kind="brightness_temperature", quantity="T9",
            units="K", valid_time="2026-01-01T00:00:00Z", data_kind="observation",
            grid=grid, source_grids=[grid], nodata="NaN",
            quality_control={"flags": {}, "valid_pixels": 1, "total_pixels": 1},
            sources=[{"kind": "synthetic_test"}], calibration_version="test-1",
            calibration_status="declared", limitations=["Синтетическая проверка."],
        )
        record = {
            "id": identity, "product": "channel", "time": "2026-01-01T00:00:00Z",
            "calibration_status": "declared", "grid": {"crs": "EPSG:3413", "width": 1, "height": 1,
                                                              "bounds": bounds},
            "transform": transform,
            "legend": {"units": "K", "version": "test-1", "nodata": "NaN",
                       "quality_flags": {}, "valid_pixels": 1, "total_pixels": 1},
            "source_grids": [grid],
            "files": ["map.png", "product.json"], "passport": passport,
        }
        record["artifacts"] = artifact_manifest(self.folder, record["files"])
        return record

    def test_accepts_matching_passport_and_exact_file_manifest(self):
        self.assertIs(validate_saved_product(self.folder, self.record), self.record)

    def test_rejects_artifact_byte_or_size_change(self):
        (self.folder / "map.png").write_bytes(b"synthetic image altered")
        with self.assertRaisesRegex(ValueError, "повреждён"):
            validate_saved_product(self.folder, self.record)

    def test_rejects_passport_record_disagreement(self):
        record = copy.deepcopy(self.record)
        record["passport"]["units"] = "DN"
        with self.assertRaisesRegex(ValueError, "противоречит"):
            validate_saved_product(self.folder, record)

    def test_rejects_traversal_and_missing_manifest_entries(self):
        record = copy.deepcopy(self.record)
        record["files"].append("../outside.tif")
        with self.assertRaisesRegex(ValueError, "небезопасно"):
            validate_saved_product(self.folder, record)
        record = copy.deepcopy(self.record)
        record["artifacts"].clear()
        with self.assertRaisesRegex(ValueError, "неполон"):
            validate_saved_product(self.folder, record)

    def test_rejects_symlinked_artifact(self):
        target = self.folder / "outside.bin"
        target.write_bytes(b"outside")
        (self.folder / "map.png").unlink()
        (self.folder / "map.png").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "повреждён"):
            validate_saved_product(self.folder, self.record)

    def test_rejects_product_directory_and_metadata_symlinks_before_read(self):
        root = self.folder.parent
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "product.json").write_text("{}")
        (root / ("b" * 24)).symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "небезопасен"):
            product_metadata_path(root, "b" * 24)
        metadata = self.folder / "product.json"
        outside_meta = root / "metadata.json"
        outside_meta.write_text("{}")
        metadata.symlink_to(outside_meta)
        with self.assertRaisesRegex(ValueError, "небезопасен"):
            product_metadata_path(root, self.folder.name)

    def test_rebuilds_legacy_cache_missing_manifest(self):
        (self.folder / "product.json").write_text("{}")
        # The legacy marker is the absent manifest; source data can safely rebuild it.
        self.assertIsNone(load_cached_product(self.folder.parent, self.folder.name))
        stage = self.folder.parent / (self.folder.name + ".building")
        stage.mkdir()
        (stage / "product.json").write_text("rebuilt")
        replace_product_cache(stage, self.folder)
        self.assertEqual((self.folder / "product.json").read_text(), "rebuilt")

    def test_rejects_non_string_file_entries_as_value_error(self):
        record = copy.deepcopy(self.record)
        record["files"] = ["product.json", []]
        with self.assertRaises(ValueError):
            validate_saved_product(self.folder, record)

    def test_rejects_non_object_legend_and_grid_as_value_error(self):
        for field, malformed in (("legend", []), ("legend", "bad"),
                                  ("grid", []), ("grid", "bad")):
            with self.subTest(field=field, malformed=malformed):
                record = copy.deepcopy(self.record)
                record[field] = malformed
                with self.assertRaises(ValueError):
                    validate_saved_product(self.folder, record)

    def test_rejects_semantic_grid_nodata_qc_and_source_grid_mismatch(self):
        mutations = (
            ("bounds", lambda p: p["grid"].update(bounds=[0, 0, 2, 2])),
            ("nodata", lambda p: p.update(nodata="unknown")),
            ("qc", lambda p: p["quality_control"].update(valid_pixels=0)),
            ("source grids", lambda p: p["source_grids"][0].update(width=2)),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                record = copy.deepcopy(self.record)
                mutate(record["passport"])
                with self.assertRaisesRegex(ValueError, "противоречит"):
                    validate_saved_product(self.folder, record)


if __name__ == "__main__":
    unittest.main()
