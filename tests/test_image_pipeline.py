import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from factory.image_pipeline import (
    CATEGORY,
    FROZEN_GEOMETRY,
    PRODUCT_SOURCE,
    background,
    compose,
    derive_white_preview,
    product_source_prompt,
    validate_product,
)
from factory.util import FactoryError


class ImagePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.product = self.root / "product_rgba_v1.png"
        self.background = self.root / "background.png"
        self._write_product(self.product)
        Image.new("RGB", (256, 256), (220, 230, 240)).save(self.background)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _write_product(path, mode="RGBA"):
        image = Image.new(mode, (128, 96), (255, 255, 255, 0) if mode == "RGBA" else (255, 255, 255))
        if mode == "RGBA":
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((20, 25, 107, 76), radius=10, fill=(244, 248, 255, 255))
            draw.rectangle((30, 67, 98, 82), fill=(120, 180, 235, 255))
        image.save(path)

    def test_only_kids_shoes_category_is_supported(self):
        with self.assertRaises(FactoryError):
            validate_product(self.product, category="office_chairs")

    def test_product_requires_real_rgba_extrema_ratios_and_bbox(self):
        image, metadata = validate_product(self.product)
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(metadata["width"], 128)
        self.assertEqual(metadata["height"], 96)
        self.assertEqual(metadata["alpha_min"], 0)
        self.assertEqual(metadata["alpha_max"], 255)
        self.assertGreater(metadata["transparent_fraction"], 0.02)
        self.assertGreater(metadata["visible_fraction"], 0.02)
        self.assertGreaterEqual(metadata["bbox"][2] - metadata["bbox"][0], 4)

        opaque = self.root / "opaque.png"
        self._write_product(opaque, mode="RGB")
        with self.assertRaises(FactoryError):
            validate_product(opaque)

        no_transparent = self.root / "no-transparent.png"
        image = Image.new("RGBA", (64, 64), (255, 255, 255, 255))
        image.save(no_transparent)
        with self.assertRaises(FactoryError):
            validate_product(no_transparent)

    def test_white_preview_and_composite_have_hash_size_and_lineage(self):
        white = self.root / "product_white_v1.png"
        preview = derive_white_preview(self.product, white)
        self.assertEqual(preview["operation"], "white_preview_v1")
        self.assertEqual(preview["output"]["width"], 128)
        self.assertEqual(preview["output"]["height"], 96)
        self.assertEqual(preview["output"]["sha256"], hashlib.sha256(white.read_bytes()).hexdigest())
        self.assertEqual(preview["lineage"]["source"]["sha256"], preview["source"]["sha256"])

        target = self.root / "candidate-outdoor.png"
        result = compose(self.product, self.background, target)
        self.assertEqual(result["algorithm"], "alpha-composite-v1")
        self.assertEqual(result["output_size"], [256, 256])
        self.assertEqual(result["processing"]["geometry"], FROZEN_GEOMETRY)
        self.assertEqual(result["output"]["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(result["lineage"]["source"]["sha256"], result["source"]["sha256"])
        self.assertEqual(result["lineage"]["background"]["sha256"], result["background"]["sha256"])
        self.assertEqual(result["lineage"]["candidate"]["sha256"], result["output"]["sha256"])

    def test_square_background_is_required_and_output_is_one_file(self):
        non_square = self.root / "non-square.png"
        Image.new("RGB", (300, 256), "white").save(non_square)
        with self.assertRaises(FactoryError):
            compose(self.product, non_square, self.root / "bad.png")

    def test_grounded_recipe_is_opt_in_and_records_its_visible_shadow(self):
        old_target = self.root / "old-candidate.png"
        new_target = self.root / "grounded-candidate.png"
        old = compose(self.product, self.background, old_target)
        new = compose(self.product, self.background, new_target, recipe="grounded-v2")
        self.assertEqual(old["algorithm"], "alpha-composite-v1")
        self.assertEqual(old["shadow"], "none")
        self.assertEqual(new["algorithm"], "alpha-composite-grounded-v2")
        self.assertGreater(new["product_size"][0], old["product_size"][0])
        self.assertEqual(new["processing"]["shadow"]["kind"], "alpha-contact-contour")
        self.assertIn("shadow", new["lineage"]["processing"])
        with Image.open(new_target) as image:
            self.assertLess(image.convert("RGB").getpixel((128, 221))[0], 220)

    def test_grounded_shadow_does_not_fill_gap_between_separate_items(self):
        source = self.root / "separated-product.png"
        image = Image.new("RGBA", (128, 96), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((8, 15, 34, 80), fill=(255, 255, 255, 255))
        draw.rectangle((94, 18, 120, 80), fill=(255, 255, 255, 255))
        image.save(source)
        target = self.root / "separated-grounded.png"
        compose(source, self.background, target, recipe="grounded-v2")
        with Image.open(target) as candidate:
            rgb = candidate.convert("RGB")
            self.assertLess(rgb.getpixel((55, 225))[0], 220)
            self.assertEqual(rgb.getpixel((128, 225)), (220, 230, 240))

    def test_prompt_recipes_are_independent_and_references_default_to_empty(self):
        self.assertIn("透明背景 PNG", PRODUCT_SOURCE)
        self.assertEqual(product_source_prompt(), PRODUCT_SOURCE)
        prompts = [background(scene) for scene in ("outdoor", "indoor", "studio")]
        self.assertEqual(len({item["prompt_hash"] for item in prompts}), 3)
        self.assertTrue(all(item["image_references"] == [] for item in prompts))

    def test_reference_requires_explicit_permission_and_immutable_hash(self):
        with self.assertRaises(FactoryError):
            background("studio", [{"use": "image_reference", "asset_id": "A", "sha256": "a" * 64}])
        with self.assertRaises(FactoryError):
            background("studio", [{"use": "description_reference", "description_source": "auto_caption", "allow_description_reuse": True}])
        result = background("studio", [{
            "use": "image_reference",
            "asset_id": "ASSET-1",
            "sha256": "a" * 64,
            "allow_image_generation": True,
        }])
        self.assertEqual(result["image_references"], [{"asset_id": "ASSET-1", "sha256": "a" * 64}])


if __name__ == "__main__":
    unittest.main()
