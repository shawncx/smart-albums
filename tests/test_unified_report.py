"""Selected user reports over synthetic saved data, without models or originals."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import io
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))
from photography_lib import feature_inputs, unified_report, unified_search, virtual_folders
from photography_lib.config import Config, PhotographyError
from photography_lib.exports import prepare_export, write_export
from photography_lib.feature_profiles import default_profile, normalize_ocr_text
from photography_lib.fingerprints import fingerprint
from photography_lib.image_vectors import pack_vector
from photography_lib.sqlite_storage import SQLiteStorage


EMBEDDING = {
    "profile_schema": "image-embedding-profile-v1", "embedding_kind": "image_text_semantic",
    "stored_modality": "image", "input_scope": "stored_thumbnail", "granularity": "whole_image",
    "model": "unified-report-fixture", "dimensions": 3, "dtype": "float32-le", "normalized": True,
}


class Page(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags, self.text = [], []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


class UnifiedReportTests(unittest.TestCase):
    def setUp(self):
        self.root = PROJECT / (".unified-report-tests-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.config = Config(self.root / "album.sqlite", model_cache_root=self.root / "no-models")
        self.store = SQLiteStorage.create(self.config.database_path)
        self.addCleanup(self.store.close)
        self.output = self.root / "report.html"
        self.encoder_calls = []
        self.embedding_id = self.store.put_embedding_profile(EMBEDDING)
        self.store.set_default_embedding_profile(self.embedding_id)
        self.profiles = {}
        for pid, vector in zip(("a", "b", "c"), ((1., 0., 0.), (.6, .8, 0.), (0., 1., 0.))):
            self.photo(pid)
            self.vector(pid, vector)

    def photo(self, pid):
        image = io.BytesIO()
        Image.new("RGB", (12, 6), "navy").save(image, "JPEG")
        photo = {
            "photo_id": pid, "content_version": hashlib.sha256(pid.encode()).hexdigest(),
            "thumbnail_profile": "fixture-preview",
            "original_absolute_path": str(self.root / "absent" / (pid + ".jpg")),
            "size_bytes": 1, "mtime_ns": 1, "metadata": {"display_width": 12, "display_height": 6},
            "ingest_state": "available", "original_status": "missing",
            "created_at": "2026-09-08T00:00:00Z", "updated_at": "2026-09-08T00:00:00Z",
        }
        self.store.put_photo(photo)
        self.store.put_thumbnail(photo, image.getvalue())

    def vector(self, pid, values):
        raw = pack_vector(values, 3)
        self.store._put_embedding_result(
            {**self.store.photo(pid),
             "input_image_hash": self.store.thumbnail(pid, include_data=False)["image_hash"]},
            self.embedding_id, raw, hashlib.sha256(raw).hexdigest(), 3)

    def put(self, pid, component, payload):
        if component not in self.profiles:
            profile = default_profile(component)
            self.store.set_default_feature_profile(component, self.store.put_feature_profile(profile))
            self.profiles[component] = profile
        profile = self.profiles[component]
        self.store.put_feature_result(pid, fingerprint(profile),
                                     feature_inputs.manifest_for(pid, profile, store=self.store), payload)

    def ocr(self, pid, text):
        self.put(pid, "ocr", {
            "width": 12, "height": 6, "complete": True, "text": text,
            "normalized_text": normalize_ocr_text(text),
            "blocks": [{"text": text, "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]],
                        "recognition_score": .9, "detection_score": None}] if text else [],
        })

    def encoder(self, profile):
        self.assertEqual(profile, EMBEDDING)

        def encode(text):
            self.encoder_calls.append(text)
            return SimpleNamespace(vector=[1., 0., 0.])

        return SimpleNamespace(profile=lambda: deepcopy(profile), encode_text=encode)

    def query(self, conditions=None, **options):
        raw = {"query": "a beach", "conditions": conditions or [{"id": "S", "kind": "semantic", "query": "a beach"}]}
        raw.update(options)
        return unified_search.query(raw, store=self.store, config=self.config, encoder_factory=self.encoder)

    def select(self, snapshot, numbers):
        return unified_search.select(snapshot, numbers, review_id=snapshot["review_id"], store=self.store)

    @contextmanager
    def report_only(self):
        before = self.config.database_path.read_bytes()

        def authorizer(action, *_):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store.db.set_authorizer(authorizer)
        try:
            with patch("photography_lib.feature_inputs.resolve_original", side_effect=AssertionError("No originals")), \
                    patch("photography_lib.feature_inputs.load_input", side_effect=AssertionError("No image inference")), \
                    patch("photography_lib.siglip_embedding.SiglipEncoder", side_effect=AssertionError("No models")), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("No downloads")), \
                    patch.object(self.store, "thumbnail", wraps=self.store.thumbnail) as thumbnails:
                yield thumbnails
        finally:
            self.store.db.set_authorizer(None)
        self.assertEqual(self.config.database_path.read_bytes(), before)

    def render(self, snapshot):
        destination = unified_report.unified_report(
            snapshot, self.output, config=self.config, store=self.store)
        self.assertEqual(destination, str(self.output.resolve()))
        return self.output.read_text(encoding="utf-8")

    def assert_passive(self, document, images, *, cards=None):
        page = Page(document)
        tags = [tag for tag, _ in page.tags]
        for tag in ("script", "form", "input", "button", "iframe", "object", "embed", "link", "details"):
            self.assertNotIn(tag, tags)
        self.assertEqual(tags.count("img"), images)
        self.assertEqual(tags.count("article"), images if cards is None else cards)
        for tag, attrs in page.tags:
            self.assertFalse(any(key.startswith("on") or key == "hidden" for key in attrs))
            self.assertNotIn("href", attrs)
            if "src" in attrs:
                self.assertEqual(tag, "img")
                self.assertTrue(attrs["src"].startswith("data:image/jpeg;base64,"))
        policies = [attrs["content"] for tag, attrs in page.tags
                    if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"]
        self.assertEqual(len(policies), 1)
        for directive in ("default-src 'none'", "img-src data:", "base-uri 'none'", "form-action 'none'"):
            self.assertIn(directive, policies[0])
        self.assertNotIn("<pre", document)
        self.assertNotIn("完整 JSON", document)

    def test_fact_projection_preserves_zero_false_and_escapes_saved_text(self):
        condition = {"kind": "object_count"}
        cell = {"raw_score": None, "evidence": {"count": 0, "internal": "PRIVATE_INTERNAL"}}
        facts = unified_report._facts(condition, cell)
        self.assertIn("<dd>0</dd>", facts)
        self.assertNotIn("PRIVATE_INTERNAL", facts)
        cell = {"raw_score": None, "evidence": {"snippet": "<script>alert('saved')</script>"}}
        facts = unified_report._facts({"kind": "ocr_contains"}, cell)
        self.assertIn("&lt;script&gt;", facts)
        self.assertNotIn("<script>", facts)

    def test_real_selected_report_has_only_selected_previews_and_relevant_evidence(self):
        self.ocr("a", "needle PRIVATE_UNSELECTED_OCR")
        self.ocr("b", "needle selected saved words")
        self.ocr("c", "needle PRIVATE_OUTSIDE_CANDIDATES")
        snapshot = self.query([
            {"id": "S", "kind": "semantic", "query": "海边", "visual_query": "a beach"},
            {"id": "T", "kind": "semantic", "query": "蓝色海水", "visual_query": "blue sea"},
            {"id": "O", "kind": "ocr_contains", "text": "needle"},
        ], query="原始：海边或蓝色海水或 needle", operator="or", candidate_limit=2)
        selected = self.select(snapshot, [2])
        with self.report_only() as thumbnails:
            document = self.render(selected)
        self.assertEqual([call.args[0] for call in thumbnails.call_args_list
                          if call.kwargs.get("include_data", True)], ["b"])
        self.assertEqual(self.encoder_calls, ["a beach", "blue sea"])
        self.assert_passive(document, 1)
        for text in ("原始：海边或蓝色海水或 needle", "OR", "实际编码文本：a beach", "实际编码文本：blue sea",
                     "查询范围 3 张", "可送审候选 3 张", "本次送审 2 张", "AI 选择展示 1 张",
                     "部分覆盖：是", "照片编号 2", "保存的余弦相似度", "needle selected saved words",
                     "不是看图核对", "unknown", "false"):
            self.assertIn(text, document)
        for token in ("PRIVATE_UNSELECTED", "PRIVATE_OUTSIDE", "scope_items", "content_version",
                      "thumbnail_profile", "vector_hash", "result_id", "input_fingerprint", "source_digest",
                      "review_id", "unified-report-fixture", str(self.config.database_path),
                      selected["snapshot_id"], selected["digest"], selected["album"]["id"]):
            self.assertNotIn(token, document)

    def test_unknown_and_false_are_distinct_and_zero_is_visible(self):
        self.put("b", "objects", {"width": 12, "height": 6, "complete": True, "objects": []})
        snapshot = self.query([
            {"id": "S", "kind": "semantic", "query": "a beach"},
            {"id": "P", "kind": "object_count", "class_id": "person", "operator": "ge", "value": 1},
        ], operator="or")
        with self.report_only():
            document = self.render(self.select(snapshot, [1, 2]))
        self.assertIn("未知（unknown）：缺少索引", document)
        self.assertIn("已保存条件为假（false）", document)
        self.assertIn("<dd>0</dd>", document)
        self.assertIn("仅有相似度证据，未经视觉确认", document)
        self.assert_passive(document, 2)

    def test_requested_supplemental_facts_are_not_presented_as_filter_clauses(self):
        self.ocr("a", "needle <b>saved text</b>")
        self.ocr("b", "")
        snapshot = self.query(evidence_conditions=[{"id": "O", "kind": "ocr_contains", "text": "needle"}])
        self.assertEqual(snapshot["candidate_count"], 3)
        selected = self.select(snapshot, [2, 1])
        with self.report_only():
            document = self.render(selected)
        self.assertIn("补充事实（不参与 AND / OR 筛选）", document)
        self.assertIn("补充事实 · OCR 包含文字：needle", document)
        self.assertIn("needle &lt;b&gt;saved text&lt;/b&gt;", document)
        self.assertIn("已保存条件为假（false）", document.split("<article>")[2])
        self.assertLess(document.index("照片编号 1"), document.index("照片编号 2"))
        self.assertEqual(self.encoder_calls, ["a beach"])
        self.assert_passive(document, 2)

    def test_explicit_empty_selection_reads_no_preview_bytes(self):
        selected = self.select(self.query(), [])
        with self.report_only() as thumbnails:
            document = self.render(selected)
        self.assertTrue(all(call.kwargs.get("include_data") is False for call in thumbnails.call_args_list))
        self.assertIn("明确选择了 0 张照片（[]）", document)
        self.assertIn("本次送审 3 张", document)
        self.assertIn("不是待筛选候选列表", document)
        self.assertNotIn("data:image/jpeg;base64,", document)
        self.assert_passive(document, 0)

    def test_pending_and_malformed_snapshots_never_render_or_overwrite(self):
        pending = self.query()
        self.output.write_bytes(b"existing report")
        for snapshot, code in ((pending, "QUERY_STAGE_INVALID"), ({}, "QUERY_SNAPSHOT_INVALID")):
            with self.subTest(code=code), self.report_only(), \
                    patch("photography_lib.unified_report._preview", side_effect=AssertionError("No previews")):
                with self.assertRaises(PhotographyError) as caught:
                    self.render(snapshot)
                self.assertEqual(caught.exception.code, code)
            self.assertEqual(self.output.read_bytes(), b"existing report")

    def test_wrong_album_rejected_before_any_preview(self):
        selected = self.select(self.query(), [1])
        other = SQLiteStorage.create(self.root / "other.sqlite")
        self.addCleanup(other.close)
        with patch.object(other, "thumbnail", side_effect=AssertionError("No previews")):
            with self.assertRaises(PhotographyError) as caught:
                unified_report.unified_report(selected, self.output, config=self.config, store=other)
        self.assertEqual(caught.exception.code, "ALBUM_MISMATCH")
        self.assertFalse(self.output.exists())

    def test_changed_saved_vector_rejected_without_output_or_previews(self):
        selected = self.select(self.query(), [1])
        self.vector("a", (0., 0., 1.))
        self.output.write_bytes(b"keep prior report")
        with self.report_only(), \
                patch("photography_lib.unified_report._preview", side_effect=AssertionError("No previews")):
            with self.assertRaises(PhotographyError) as caught:
                self.render(selected)
        self.assertEqual(caught.exception.code, "QUERY_SNAPSHOT_STALE")
        self.assertEqual(self.output.read_bytes(), b"keep prior report")

    def test_unselected_vector_change_does_not_block_frozen_selected_report(self):
        selected = self.select(self.query(), [1])
        self.vector("b", (0., 0., 1.))
        with self.report_only():
            document = self.render(selected)
        self.assertEqual(document.count('src="data:image/jpeg;base64,'), 1)

    def test_html_escapes_original_query_encoded_text_album_scope_and_ocr(self):
        injected = "<script>alert('saved & \"quoted\"')</script>"
        self.ocr("a", "needle " + injected)
        folder = virtual_folders.create_folder(injected, store=self.store)["folder"]["folder_id"]
        virtual_folders.add_photos(folder, ["a"], store=self.store)
        album = {**self.store.album(), "name": injected}
        with patch.object(self.store, "album", return_value=album):
            snapshot = self.query([
                {"id": "<S>", "kind": "semantic", "query": "图像", "visual_query": injected},
                {"id": "O", "kind": "ocr_contains", "text": "needle"},
            ], query=injected, operator="and", scope={"folder_ids": [folder]})
        selected = self.select(snapshot, [1])
        virtual_folders.rename_folder(folder, "CURRENT_NAME_NOT_IN_FROZEN_SCOPE", store=self.store)
        with self.report_only():
            document = self.render(selected)
        self.assert_passive(document, 1)
        self.assertIn("&lt;script&gt;alert(&#x27;saved &amp; &quot;quoted&quot;&#x27;)&lt;/script&gt;", document)
        self.assertIn("&lt;S&gt;", document)
        self.assertIn("AND", document)
        self.assertIn("查询时文件夹范围（并集）", document)
        self.assertNotIn("CURRENT_NAME_NOT_IN_FROZEN_SCOPE", document)

    def test_unavailable_selected_preview_shows_placeholder_not_replacement(self):
        selected = self.select(self.query(), [1])
        self.store.db.execute("UPDATE thumbnails SET data=? WHERE photo_id='a'", (b"broken JPEG",))
        with self.report_only() as thumbnails:
            document = self.render(selected)
        self.assertIn("预览不可用", document)
        self.assertIn("照片编号 1", document)
        self.assertIn("保存的余弦相似度", document)
        self.assertEqual([call.args[0] for call in thumbnails.call_args_list
                          if call.kwargs.get("include_data", True)], ["a"])
        self.assert_passive(document, 0, cards=1)

    def test_missing_preview_keeps_selected_saved_facts_visible(self):
        self.store.put_photo({**self.store.photo("b"), "content_version": self.store.photo("a")["content_version"]})
        self.store.db.execute("DELETE FROM thumbnails WHERE photo_id IN ('a','b')")
        selected = self.select(self.query([
            {"id": "D", "kind": "has_near_duplicate", "metric": "exact"},
        ]), [1])
        with self.report_only() as thumbnails:
            document = self.render(selected)
        self.assertTrue(all(call.kwargs.get("include_data") is False for call in thumbnails.call_args_list))
        self.assertIn("预览不可用", document)
        self.assertIn("最小重复距离", document)
        self.assertIn("<dd>0</dd>", document)
        self.assertNotIn("peer_id", document)
        self.assert_passive(document, 0, cards=1)

    def test_other_fact_kinds_have_explicit_user_facing_labels(self):
        fixtures = (
            ({"kind": "color_fraction", "color": "blue", "minimum": .5},
             {"fraction": .75, "rule_version": "PRIVATE_RULE"}, "保存的颜色比例", "0.75"),
            ({"kind": "subject_position", "horizontal": "left", "vertical": None},
             {"center_x": 0., "center_y": .25}, "主体横向中心", "0.25"),
            ({"kind": "scene", "scene_id": "beach", "minimum": .5},
             {"score": .8}, "保存的场景相似度", "0.8"),
            ({"kind": "has_near_duplicate", "metric": "hamming", "max_distance": 8},
             {"best_distance": 0, "peer_count": 1, "peer_id": "PRIVATE_PEER"}, "最小重复距离", "0"),
        )
        for condition, evidence, label, value in fixtures:
            with self.subTest(kind=condition["kind"]):
                self.assertTrue(unified_report._condition_label(condition))
                facts = unified_report._facts(condition, {"raw_score": None, "evidence": evidence})
                self.assertIn(label, facts)
                self.assertIn("<dd>" + value + "</dd>", facts)
                self.assertNotIn("PRIVATE", facts)

    def test_no_semantic_inputs_does_not_claim_text_was_encoded(self):
        self.store.db.execute("DELETE FROM image_embedding_results")
        selected = self.select(self.query(), [])
        with self.report_only():
            document = self.render(selected)
        self.assertEqual(self.encoder_calls, [])
        self.assertIn("本次未执行文字编码", document)
        self.assertNotIn("实际编码文本：", document)
        self.assertIn("可送审候选 0 张", document)
        self.assert_passive(document, 0)

    def test_structural_only_report_needs_no_encoder(self):
        self.ocr("a", "needle")
        selected = self.select(self.query([{"id": "O", "kind": "ocr_contains", "text": "needle"}]), [1])
        with self.report_only():
            document = self.render(selected)
        self.assertEqual(self.encoder_calls, [])
        self.assertIn("本次只使用已保存的结构化事实，没有文字编码", document)
        self.assertIn("已保存条件为真（true）", document)
        self.assert_passive(document, 1)

    def test_safe_export_rejects_protected_inputs_and_wrong_suffix(self):
        original = self.root / "registered-original.html"
        original.write_bytes(b"registered original")
        self.store.put_photo({**self.store.photo("a"), "original_absolute_path": str(original)})
        selected = self.select(self.query(), [1])
        self.config.model_cache_root.mkdir()
        cached = self.config.model_cache_root / "model.html"
        cached.write_bytes(b"saved cache")
        protected = (original, cached, self.config.database_path)
        before = {path: path.read_bytes() for path in protected}
        with patch("photography_lib.unified_report._preview", side_effect=AssertionError("No previews")):
            for output in (*protected, self.root / "wrong.json"):
                with self.subTest(output=output), self.assertRaises(PhotographyError) as caught:
                    unified_report.unified_report(selected, output, config=self.config, store=self.store)
                self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        self.assertFalse((self.root / "wrong.json").exists())

    def test_prepared_export_keeps_the_original_target_identity(self):
        selected = self.select(self.query(), [1])
        target = prepare_export(self.output, self.config, self.store, (".html",))
        with self.report_only(), \
                patch("photography_lib.exports.export_path", side_effect=AssertionError("Do not resolve again")), \
                patch("photography_lib.unified_report.write_export", wraps=write_export) as publish:
            result = unified_report.unified_report(selected, target, config=self.config, store=self.store)
        self.assertIs(publish.call_args.args[0], target)
        self.assertEqual(result, str(self.output.resolve()))
        self.assert_passive(self.output.read_text(encoding="utf-8"), 1)

    def test_atomic_write_failure_preserves_existing_output(self):
        selected = self.select(self.query(), [1])
        self.output.write_bytes(b"prior complete report")
        with self.report_only(), patch("photography_lib.exports.os.replace", side_effect=OSError("publish failed")):
            with self.assertRaises(PhotographyError) as caught:
                self.render(selected)
        self.assertEqual(caught.exception.code, "EXPORT_FAILED")
        self.assertEqual(self.output.read_bytes(), b"prior complete report")
        self.assertEqual(list(self.root.glob(".smart-albums-export-*")), [])
