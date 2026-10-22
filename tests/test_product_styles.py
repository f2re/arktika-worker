import copy
import tempfile
import unittest
from pathlib import Path

from arktika.product_styles import (
    ProductStyleStore,
    default_profile,
    validate_profile,
    validate_styleable_product,
)


def make_product(product_id="product-a", algorithm_version="0.2.3"):
    return {
        "id": product_id,
        "product": "difference",
        "files": ["map.png", "values.tif"],
        "passport": {"algorithm": {"version": algorithm_version}},
        "legend": {
            "quantity_kind": "temperature_difference",
            "units": "K",
            "display_min": -4,
            "display_max": 2,
            "palette": [[25, 46, 88], [47, 170, 194], [158, 30, 65]],
        },
    }


class ProductStyleTests(unittest.TestCase):
    def test_default_comes_only_from_its_product_legend(self):
        product = make_product()
        before = copy.deepcopy(product)
        profile = default_profile(product)
        self.assertEqual(profile["minimum"], -4.0)
        self.assertEqual(profile["maximum"], 2.0)
        self.assertEqual(profile["palette"], product["legend"]["palette"])
        self.assertEqual(profile["product_id"], "product-a")
        self.assertEqual(product, before)

    def test_rejects_archive_rgb_and_categorical_products(self):
        archive = make_product()
        archive.update(product="archive_rgb")
        archive["legend"]["quantity_kind"] = "rgb"
        with self.assertRaisesRegex(ValueError, "RGB-композиции"):
            validate_styleable_product(archive)

        categorical = make_product()
        categorical["legend"]["quantity_kind"] = "categorical"
        with self.assertRaisesRegex(ValueError, "непрерывных"):
            default_profile(categorical)

    def test_stored_values_are_stylable_without_physical_relabeling(self):
        stored = make_product()
        stored["legend"].update(quantity_kind="stored_value", units="DN")
        profile = default_profile(stored)
        self.assertEqual(profile["product_id"], stored["id"])
        self.assertEqual(stored["legend"]["units"], "DN")

    def test_requires_values_tiff_and_valid_legend_defaults(self):
        product = make_product()
        product["files"] = ["map.png"]
        with self.assertRaisesRegex(ValueError, "values.tif"):
            default_profile(product)
        product = make_product()
        product["legend"]["display_min"] = float("nan")
        with self.assertRaisesRegex(ValueError, "конечным"):
            default_profile(product)

    def test_validation_binds_profile_to_product_and_algorithm_version(self):
        product = make_product()
        profile = default_profile(product)
        profile.update(minimum=-8, maximum=5, palette_id="blue-orange")
        self.assertEqual(validate_profile(product, profile)["minimum"], -8.0)
        wrong_product = dict(profile, product_id="another-product")
        with self.assertRaisesRegex(ValueError, "другому продукту"):
            validate_profile(product, wrong_product)
        wrong_version = dict(profile, algorithm_version="0.2.4")
        with self.assertRaisesRegex(ValueError, "другой версии"):
            validate_profile(product, wrong_version)

    def test_invalid_range_palette_and_scale_are_rejected(self):
        product = make_product()
        default = default_profile(product)
        for changes, message in (
            ({"minimum": 2, "maximum": 2}, "меньше"),
            ({"minimum": float("inf")}, "конечным"),
            ({"palette": [[0, 0, 0]]}, "не менее двух"),
            ({"palette": [[0, 0, 0], [0, 0, 256]]}, "0–255"),
            ({"scale_mode": "log"}, "только линейная"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                validate_profile(product, dict(default, **changes))

    def test_profiles_are_durable_and_product_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProductStyleStore(Path(directory))
            first = make_product("same-type-a")
            second = make_product("same-type-b")
            profile = dict(default_profile(first), minimum=-7, maximum=4)
            store.save(first, profile)
            self.assertEqual(store.get(first)["minimum"], -7.0)
            self.assertEqual(store.get(second)["minimum"], -4.0)

            newer_algorithm = make_product("same-type-a", "0.2.4")
            self.assertEqual(store.get(newer_algorithm)["minimum"], -4.0)
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
