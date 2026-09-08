from copy import deepcopy
import re
import unittest
from unittest.mock import patch

from tests.duplicate_fixtures import DuplicateFixture
from photography_lib.duplicates_report import duplicate_report


class DuplicateReportTests(DuplicateFixture, unittest.TestCase):
    def report(self, snapshot):
        output = self.root / "report.html"
        duplicate_report(snapshot, output, config=self.config, store=self.store)
        return output.read_text(encoding="utf-8")

    def test_grouped_report_has_every_photo_once_with_original_metadata(self):
        for pid, value in (("a", 0), ("b", 1), ("c", 3)):
            self.photo(pid)
            self.hash(pid, value)
        snapshot = self.scan(mode="similar", max_distance=1)
        text = self.report(snapshot)
        self.assertEqual(re.findall(r'<article data-photo-id="([^"]+)"', text), ["a", "b", "c"])
        self.assertEqual(text.count("data:image/jpeg;base64,"), 3)
        self.assertIn("6000 × 4000", text)
        self.assertNotIn("36 × 24", text)
        self.assertIn("同组不代表任意两张都匹配", text)
        self.assertIn("2 对直接匹配", text)
        self.assertNotIn("<script", text)

    def test_missing_changed_and_corrupt_previews_use_placeholders(self):
        for pid in ("a", "b", "c"):
            self.photo(pid, content="same", preview=pid != "c")
        snapshot = self.scan(mode="exact")
        self.photo("a", content="changed")
        self.store.db.execute("UPDATE thumbnails SET data=? WHERE photo_id='b'", (b"broken",))
        text = self.report(snapshot)
        self.assertEqual(text.count("预览不可用"), 3)
        self.assertNotIn("data:image/jpeg;base64,", text)
        self.assertEqual(text.count("<article "), 3)

    def test_saved_text_is_escaped_and_no_metadata_is_fabricated(self):
        self.photo("a", content="same", metadata={"width": None})
        self.photo("b", content="same")
        snapshot = self.scan(mode="exact")
        snapshot["album"]["name"] = '<script>alert("album")</script>'
        snapshot["inputs"][0]["original_absolute_path"] = str(self.root / '<img src=x onerror=alert(1)>.jpg')
        snapshot["inputs"][0]["metadata"]["datetime_original"] = "<svg onload=alert(2)>"
        self.seal(snapshot)
        text = self.report(snapshot)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        self.assertNotIn("<svg ", text)
        self.assertIn("原图尺寸</dt><dd>未知", text)

    def test_report_remains_historical_after_hash_results_change(self):
        for pid in ("a", "b"):
            self.photo(pid)
            self.hash(pid, 0)
        snapshot = self.scan(mode="similar", max_distance=0)
        with patch.object(self.store, "feature_result", side_effect=AssertionError("No current feature read")), \
             patch("photography_lib.feature_index.inspect_feature", side_effect=AssertionError("No rescan")):
            text = self.report(snapshot)
        self.assertEqual(text.count("data:image/jpeg;base64,"), 2)

    def test_empty_messages_distinguish_incomplete_coverage(self):
        self.photo("a")
        text = self.report(self.scan())
        self.assertIn("检查不完整", text)
        self.assertIn("未检查部分仍可能存在重复", text)
        text = self.report(self.scan(mode="exact"))
        self.assertIn("在本次规则下未发现重复候选", text)
        self.assertNotIn("检查不完整", text)


if __name__ == "__main__":
    unittest.main()
