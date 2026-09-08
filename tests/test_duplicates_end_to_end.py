from pathlib import Path
import tempfile
import unittest

from tests.duplicates_demo import create_demo


class DuplicateEndToEndTests(unittest.TestCase):
    def test_real_ingestion_and_dhash_with_synthetic_transformed_images(self):
        with tempfile.TemporaryDirectory(prefix="duplicate-e2e-") as root:
            result = create_demo(Path(root) / "demo")
            self.assertEqual(result["summary"]["exact_pair_count"], 1)
            relations = {frozenset((pair["a"], pair["b"])): pair for pair in result["pairs"]}
            copy = relations[frozenset(("01-scene.jpg", "02-identical-copy.jpg"))]
            self.assertEqual(copy["metric"], "exact")
            resized = relations[frozenset(("01-scene.jpg", "03-resized-compressed.jpg"))]
            self.assertEqual(resized["metric"], "hamming")
            self.assertLessEqual(resized["distance"], 8)
            self.assertFalse(any("05-different-checkerboard.jpg" in pair for pair in relations))
            self.assertGreater(result["html_bytes"], result["snapshot_bytes"])


if __name__ == "__main__":
    unittest.main()
