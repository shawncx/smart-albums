"""Synthetic stage-two query tests; no model, original-image or thumbnail-byte access."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from photography_lib import condition_queries, feature_index, feature_inputs, feature_predicates
from photography_lib.condition_queries import descriptor, normalize_query, validate_cell
from photography_lib.config import PhotographyError
from photography_lib.feature_algorithms import compute_composition
from photography_lib.feature_profiles import default_profile, normalize_ocr_text
from photography_lib.feature_query_storage import ocr_matches
from photography_lib.fingerprints import fingerprint
from photography_lib.image_vectors import pack_vector
from photography_lib.sqlite_storage import SQLiteStorage
from photography_lib.virtual_folders import create_folder, add_photos, resolve_scope


EMBEDDING = {
    "profile_schema": "image-embedding-profile-v1", "embedding_kind": "image_text_semantic",
    "stored_modality": "image", "input_scope": "stored_thumbnail", "granularity": "whole_image",
    "model": "synthetic-query", "dimensions": 3, "dtype": "float32-le", "normalized": True,
}


class FeatureQueryTests(unittest.TestCase):
    def setUp(self):
        self.base = PROJECT / (".feature-query-tests-" + uuid4().hex)
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base)
        self.path = self.base / "query.sqlite"
        self.store = SQLiteStorage.create(self.path)
        self.addCleanup(lambda: self.store.close())
        for pid in ("a", "b", "c"):
            self.photo(pid, "duplicate" if pid in ("a", "b") else pid)
        self.saved_profiles = {}

    def photo(self, pid, content):
        stamp = "2026-09-06T00:00:00Z"
        photo = {
            "photo_id": pid, "content_version": hashlib.sha256(content.encode()).hexdigest(),
            "thumbnail_profile": "synthetic-preview-v1",
            "original_absolute_path": str(self.base / "nonexistent" / (pid + ".jpg")),
            "size_bytes": 17, "mtime_ns": 1,
            "metadata": {"display_width": 12, "display_height": 6},
            "ingest_state": "available", "original_status": "not_checked",
            "created_at": stamp, "updated_at": stamp,
        }
        self.store.put_photo(photo)
        self.store.db.execute("INSERT OR REPLACE INTO thumbnails VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (pid, photo["content_version"], photo["thumbnail_profile"],
                              hashlib.sha256(b"not a photo").hexdigest(), "image/jpeg",
                              12, 6, stamp, 11, b"not a photo"))
        return photo

    def profile(self, component, dependency=None, **parameters):
        profile = default_profile(component, dependency_profile_id=dependency)
        profile["parameters"].update(parameters)
        profile_id = self.store.put_feature_profile(profile)
        self.store.set_default_feature_profile(component, profile_id)
        self.saved_profiles[component] = profile_id
        return profile_id

    def put(self, pid, component, payload, *, profile_id=None):
        profile_id = profile_id or self.saved_profiles.get(component) or self.profile(component)
        profile = self.store.feature_profile(profile_id)
        manifest = feature_inputs.manifest_for(pid, profile, store=self.store)
        return self.store.put_feature_result(
            pid, profile_id, manifest, payload,
            feature_dependencies=[manifest["source_result_id"]] if component == "composition" else [],
            embedding_dependencies=[manifest["embedding_result_id"]] if component == "scene" else [])

    def ocr(self, pid, text, *, complete=True, profile_id=None):
        blocks = [{"text": block, "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]],
                   "recognition_score": None, "detection_score": None} for block in text.split("\n")] if text else []
        return self.put(pid, "ocr", {"width": 12, "height": 6, "complete": complete,
                                     "text": text, "normalized_text": normalize_ocr_text(text), "blocks": blocks},
                        profile_id=profile_id)

    def objects(self, pid, items=(), *, complete=True):
        return self.put(pid, "objects", {"width": 12, "height": 6, "complete": complete, "objects": list(items)})

    def color(self, pid, fraction, *, complete=True):
        palette = [{"rgb": [0, 0, 255], "fraction": fraction}] if fraction else []
        if fraction < 1:
            palette.append({"rgb": [255, 0, 0], "fraction": 1 - fraction})
        return self.put(pid, "color", {"width": 12, "height": 6, "complete": complete,
                                      "features": {"mean_saturation": 1, "low_saturation_fraction": 0,
                                                   "hue_histogram": [1, *([0] * 11)]},
                                      "palette": palette})

    def hash(self, pid, value, *, complete=True, profile_id=None):
        return self.put(pid, "perceptual_hash", {"complete": complete, "algorithm": "dhash", "bits": 64,
                                               "hash_hex": f"{value:016x}"}, profile_id=profile_id)

    def scene(self, pid, score):
        embedding_id = self.store.put_embedding_profile(EMBEDDING)
        profile_id = self.saved_profiles.get("scene") or self.profile("scene", embedding_id)
        profile = self.store.feature_profile(profile_id)
        if self.store.scene_prototype_set(profile_id) is None:
            self.store.put_scene_prototypes(profile_id, [
                {**scene, "vector": [1., 0., 0.]} for scene in profile["parameters"]["catalog"]])
        photo = self.store.photo(pid)
        thumbnail = self.store.thumbnail(pid, include_data=False)
        blob = pack_vector([1, 0, 0], 3)
        self.store._put_embedding_result(
            {**photo, "input_image_hash": thumbnail["image_hash"]}, embedding_id,
            blob, hashlib.sha256(blob).hexdigest(), 3)
        return self.put(pid, "scene", {"complete": True, "scores": [
            {"scene_id": scene["scene_id"], "score": score} for scene in profile["parameters"]["catalog"]]})

    def composition(self, pid):
        objects_id = self.saved_profiles["objects"]
        profile_id = self.saved_profiles.get("composition") or self.profile("composition", objects_id)
        profile = self.store.feature_profile(profile_id)
        _, record = feature_index.inspect_feature(pid, self.store.feature_profile(objects_id), store=self.store)
        return self.put(pid, "composition", compute_composition(record["payload"], profile))

    @contextmanager
    def readonly(self):
        blocked = []
        writes = {getattr(sqlite3, name) for name in (
            "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE",
            "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER",
            "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW",
            "SQLITE_CREATE_VTABLE", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE", "SQLITE_DROP_TRIGGER",
            "SQLITE_DROP_VIEW", "SQLITE_DROP_VTABLE", "SQLITE_ALTER_TABLE", "SQLITE_ATTACH", "SQLITE_DETACH")}

        def authorize(action, table, column, database, trigger):
            if action in writes or (action == sqlite3.SQLITE_READ and table == "thumbnails" and column == "data"):
                blocked.append((action, table, column))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store.db.set_authorizer(authorize)
        try:
            with patch("photography_lib.feature_inputs.resolve_original", side_effect=AssertionError("No originals")), \
                 patch("photography_lib.feature_inputs.load_input", side_effect=AssertionError("No pixel loading")), \
                 patch("photography_lib.feature_index._vision_provider", side_effect=AssertionError("No models")), \
                 patch("urllib.request.urlopen", side_effect=AssertionError("No network")), \
                 self.store.read_snapshot():
                yield blocked
        finally:
            self.store.db.set_authorizer(None)
        self.assertEqual(blocked, [])

    def query(self, *conditions, ids=None):
        with self.readonly():
            normalized = normalize_query({"conditions": list(conditions)}, store=self.store)["query"]
            photos = self.store.photos() if ids is None else [self.store.photo(pid) for pid in ids]
            matrix = feature_predicates.evaluate_conditions(normalized["conditions"], photos, store=self.store)
            for row in matrix.values():
                for condition in normalized["conditions"]:
                    self.assertNotIn("normalized_score", row[condition["id"]])
                    validate_cell(condition, row[condition["id"]])
            return matrix

    def test_normalization_freezes_defaults_deduplicates_and_is_idempotent(self):
        ocr_id = self.profile("ocr")
        raw = {"conditions": [
            {"id": "A", "kind": "ocr_contains", "text": " ＣＡＦÉ\t中国 "},
            {"id": "B", "kind": "ocr_contains", "text": "café 中国", "profile_id": ocr_id},
        ], "scope": {"folder_ids": ["z", "a", "z"], "match": "union"}}
        saved = deepcopy(raw)
        first = normalize_query(raw, store=self.store)
        self.assertEqual(raw, saved)
        self.assertEqual(first["aliases"], {"A": "A", "B": "A"})
        canonical = first["query"]
        self.assertEqual(canonical["scope"], {"folder_ids": ["a", "z"], "match": "union"})
        self.assertEqual(canonical["conditions"][0]["text"], "café 中国")
        self.assertEqual(canonical["conditions"][0]["profile_id"], ocr_id)
        self.assertEqual(normalize_query(canonical, store=self.store)["query"], canonical)
        self.profile("ocr", text_score=.7)
        self.assertEqual(normalize_query(canonical, store=self.store)["query"], canonical)

    def test_semantic_only_normalizes_and_uses_existing_embedding_profile(self):
        profile_id = self.store.put_embedding_profile(EMBEDDING)
        self.store.set_default_embedding_profile(profile_id)
        with self.readonly():
            result = normalize_query({"conditions": [{"id": "S", "kind": "semantic", "query": "  海边  "}]},
                                     store=self.store)["query"]
        self.assertEqual(result["conditions"][0], {
            "id": "S", "kind": "semantic", "query": "海边", "profile_id": profile_id, "scoring": "graded"})
        self.assertEqual(descriptor(result["conditions"][0]), {"score_type": "cosine", "direction": "higher"})
        with self.assertRaises(PhotographyError):
            feature_predicates.evaluate_conditions(result["conditions"], [], store=self.store)

    def test_query_rejects_unknown_fields_invalid_ids_and_bad_bounds(self):
        self.profile("ocr")
        condition = {"id": "A", "kind": "ocr_contains", "text": "北京"}
        invalid = [
            {"sql": "select 1"}, {"schema": "other"}, {"operator": "and"},
            {"conditions": []}, {"conditions": [condition, condition]},
            {"conditions": [{**condition, "id": "A B"}]}, {"conditions": [{**condition, "id": None}]},
            {"conditions": [{**condition, "kind": "caption"}]}, {"conditions": [{**condition, "text": "\x00"}]},
            {"conditions": [{**condition, "pixels": "private"}]}, {"conditions": [{**condition, "minimum": 0}]},
            {"conditions": [{**condition, "scoring": "graded"}]},
            {"scope": {"folder_ids": ["a", "b"]}}, {"scope": {"folder_ids": [], "match": "union"}},
            {"scope": {"folder_ids": ["a"], "match": ""}}, {"scope": {"folder_ids": "a"}},
            {"scope": {"folder_ids": [False]}}, {"scope": {"folder_ids": ["a"], "sql": "x"}},
            {"semantic_candidates": True}, {"semantic_candidates": 0}, {"semantic_candidates": 1.5},
            {"review_page_size": 0}, {"review_page_size": 1001}, {"review_page_size": True},
            {"random_seed": 17}, {"random_seed": ""}, {"random_seed": "a" * 257},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(PhotographyError):
                normalize_query({"conditions": [condition], **changes}, store=self.store)
        result = normalize_query({"conditions": [condition], "semantic_candidates": "all", "review_page_size": 1000},
                                 store=self.store)["query"]
        self.assertEqual(result["semantic_candidates"], "all")

    def test_configuration_capabilities_and_thresholds_rejected_up_front_even_empty_scope(self):
        condition = {"id": "N", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 2}
        with self.assertRaises(PhotographyError) as raised:
            normalize_query({"conditions": [condition]}, store=self.store)
        self.assertEqual(raised.exception.code, "FEATURE_CONFIGURATION_REQUIRED")
        objects_id = self.profile("objects", score_threshold=.4)
        color_id = self.profile("color")
        for changes in ({"class_id": "dragon"}, {"score_threshold": .3999}, {"score_threshold": float("nan")},
                        {"score_threshold": True}, {"value": -1}, {"value": 2.0}, {"operator": "gt"},
                        {"profile_id": color_id}, {"profile_id": "absent"}):
            with self.subTest(changes=changes), self.assertRaises(PhotographyError):
                feature_predicates.evaluate_conditions([{**condition, **changes}], [], store=self.store)
        normalized = normalize_query({"conditions": [condition]}, store=self.store)["query"]["conditions"][0]
        self.assertEqual(normalized["score_threshold"], .4)
        self.assertEqual(normalized["profile_id"], objects_id)
        for minimum in (None, -0.1, 1.1, float("inf"), float("nan"), True, 10 ** 10000):
            with self.subTest(type=type(minimum).__name__), self.assertRaises(PhotographyError):
                normalize_query({"conditions": [{"id": "C", "kind": "color_fraction", "color": "blue",
                                                   "minimum": minimum}]}, store=self.store)

    def test_object_counts_empty_incomplete_and_or_statuses_remain_independent(self):
        item = {"class_id": "person", "score": .3, "bbox": [0, 0, 1, 1]}
        self.objects("a", [item, {**item, "score": .9}])
        self.objects("b")
        self.objects("c", complete=False)
        self.ocr("c", "中国")
        matrix = self.query(
            {"id": "two", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 2},
            {"id": "zero", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 0},
            {"id": "high", "kind": "object_count", "class_id": "person", "operator": "ge", "value": 1,
             "score_threshold": .8},
            {"id": "le", "kind": "object_count", "class_id": "person", "operator": "le", "value": 1},
            {"id": "text", "kind": "ocr_contains", "text": "中国"})
        self.assertEqual(matrix["a"]["two"]["raw_score"], 1)
        self.assertEqual(matrix["a"]["high"]["evidence"]["count"], 1)
        self.assertEqual(matrix["a"]["le"]["status"], "not_matched")
        self.assertEqual(matrix["b"]["zero"]["status"], "matched")
        self.assertEqual(matrix["c"]["zero"]["status"], "unknown_invalid")
        self.assertEqual(matrix["c"]["text"]["status"], "matched")
        self.assertEqual(matrix["a"]["text"]["status"], "unknown_missing_index")
        self.assertIsNone(matrix["b"]["two"]["raw_score"])

    def test_multiple_conditions_reuse_current_result_decode_by_photo_profile(self):
        self.objects("a")
        conditions = [{"id": str(index), "kind": "object_count", "class_id": "person",
                       "operator": "eq", "value": index} for index in range(12)]
        with patch.object(feature_index, "inspect_feature", wraps=feature_index.inspect_feature) as inspect, \
             patch.object(self.store, "feature_result", wraps=self.store.feature_result) as decode:
            self.query(*conditions)
        self.assertEqual(inspect.call_count, 3)
        self.assertEqual(decode.call_count, 1)

    def test_ocr_chinese_one_two_three_characters_and_literal_punctuation(self):
        text = '中国人 来北京 ＣＡＦÉ 100% _ A"B OR NOT * : ( )\n中国人'
        self.ocr("a", text)
        self.ocr("b", "unrelated abc")
        self.ocr("c", "")
        terms = ["中", "中国", "中国人", "北京", "café", "%", "_", 'a"b', "or not", "* :", "( )"]
        conditions = [{"id": str(index), "kind": "ocr_contains", "text": term} for index, term in enumerate(terms)]
        statements = []
        self.store.db.set_trace_callback(statements.append)
        try:
            matrix = self.query(*conditions)
        finally:
            self.store.db.set_trace_callback(None)
        self.assertTrue(all(cell["status"] == "matched" for cell in matrix["a"].values()))
        self.assertTrue(all(cell["status"] == "not_matched" for cell in matrix["b"].values()))
        self.assertTrue(all(cell["status"] == "not_matched" for cell in matrix["c"].values()))
        lookup = [sql for sql in statements if "AS snippet" in sql]
        self.assertTrue(any("image_ocr_fts MATCH" in sql for sql in lookup))
        self.assertTrue(any("image_ocr_fts MATCH" not in sql for sql in lookup))
        self.assertTrue(all("instr(d.normalized_text" in sql for sql in lookup))

    def test_ocr_quoted_fts_does_not_expand_syntax_and_snippets_are_bounded(self):
        self.ocr("a", "prefix " * 100 + 'needle OR "other"' + " suffix" * 100)
        self.ocr("b", "needle")
        self.ocr("c", "other")
        matrix = self.query({"id": "literal", "kind": "ocr_contains", "text": 'needle OR "other"'})
        self.assertEqual(matrix["a"]["literal"]["status"], "matched")
        self.assertEqual(len(matrix["a"]["literal"]["evidence"]["snippet"]), 160)
        self.assertEqual(matrix["b"]["literal"]["status"], "not_matched")
        self.assertEqual(matrix["c"]["literal"]["status"], "not_matched")
        self.assertNotIn("prefix prefix prefix prefix prefix prefix prefix prefix", json.dumps(matrix))
        with self.readonly():
            self.assertEqual(ocr_matches("needle", [], store=self.store), {})

    def test_ocr_history_other_profiles_empty_and_incomplete_do_not_become_negative_coverage(self):
        old_id = self.profile("ocr")
        self.ocr("a", "needle", profile_id=old_id)
        self.ocr("b", "needle", complete=False, profile_id=old_id)
        self.photo("a", "changed")
        new_id = self.profile("ocr", text_score=.8)
        self.ocr("c", "needle", profile_id=new_id)
        matrix = self.query({"id": "old", "kind": "ocr_contains", "text": "needle", "profile_id": old_id},
                            {"id": "new", "kind": "ocr_contains", "text": "needle"})
        self.assertEqual(matrix["a"]["old"]["status"], "unknown_stale")
        self.assertEqual(matrix["b"]["old"]["status"], "unknown_invalid")
        self.assertEqual(matrix["c"]["old"]["status"], "unknown_missing_index")
        self.assertEqual(matrix["c"]["new"]["status"], "matched")
        self.ocr("a", "", profile_id=old_id)
        matrix = self.query({"id": "old", "kind": "ocr_contains", "text": "needle", "profile_id": old_id})
        self.assertEqual(matrix["a"]["old"]["status"], "not_matched")

    def test_color_fraction_exact_and_graded_thresholds_and_palette_boundaries(self):
        self.color("a", .2)
        self.color("b", .8)
        self.color("c", 0)
        base = {"kind": "color_fraction", "color": "blue", "minimum": .2}
        matrix = self.query({"id": "graded", **base})
        exact = self.query({"id": "exact", **base, "scoring": "exact"})
        self.assertEqual(matrix["a"]["graded"]["raw_score"], .2)
        self.assertEqual(matrix["b"]["graded"]["raw_score"], .8)
        self.assertEqual(exact["a"]["exact"]["raw_score"], exact["b"]["exact"]["raw_score"])
        with self.assertRaisesRegex(PhotographyError, "same scoring policy"):
            self.query({"id": "graded", **base}, {"id": "exact", **base, "scoring": "exact"})
        self.assertEqual(matrix["c"]["graded"]["status"], "not_matched")
        self.assertEqual(matrix["c"]["graded"]["raw_score"], 0)
        self.assertEqual(matrix["a"]["graded"]["evidence"]["rule_version"], "hsv-palette-v1")
        colors = {
            (0, 0, 0): "black", (255, 255, 255): "white", (128, 128, 128): "gray",
            (255, 0, 0): "red", (255, 128, 0): "orange", (255, 255, 0): "yellow",
            (0, 255, 0): "green", (0, 255, 255): "cyan", (0, 0, 255): "blue",
            (128, 0, 255): "purple", (255, 0, 255): "magenta",
            (50, 50, 50): "black", (51, 51, 51): "gray", (216, 216, 216): "gray",
            (217, 217, 217): "white",
        }
        for rgb, expected in colors.items():
            self.assertEqual(feature_predicates.palette_color(rgb), expected)

    def test_composition_thirds_combined_axes_absent_subject_and_stale_dependency(self):
        for pid, box in (("a", [0, 0, 2 / 3, 2 / 3]), ("b", [1 / 3, 1 / 3, 1, 1])):
            self.objects(pid, [{"class_id": "person", "score": .9, "bbox": box}])
            self.composition(pid)
        self.objects("c")
        self.composition("c")
        matrix = self.query({"id": "mid", "kind": "subject_position", "horizontal": "center", "vertical": "middle"},
                            {"id": "right", "kind": "subject_position", "horizontal": "right", "vertical": "bottom"})
        self.assertEqual(matrix["a"]["mid"]["status"], "matched")
        self.assertEqual(matrix["b"]["right"]["status"], "matched")
        self.assertEqual(matrix["a"]["right"]["status"], "not_matched")
        self.assertEqual(matrix["c"]["right"]["status"], "unknown_invalid")
        self.assertEqual(matrix["c"]["right"]["reason"], "no_primary_subject")
        self.photo("a", "changed")
        matrix = self.query({"id": "pos", "kind": "subject_position", "horizontal": "right"})
        self.assertEqual(matrix["a"]["pos"]["status"], "unknown_stale")

    def test_scene_saved_catalog_graded_scores_explicit_minimum_and_stale_embedding(self):
        self.scene("a", .2)
        self.scene("b", .8)
        self.scene("c", -.4)
        base = {"kind": "scene", "scene_id": "beach", "minimum": .2}
        matrix = self.query({"id": "grade", **base})
        exact = self.query({"id": "exact", **base, "scoring": "exact"})
        self.assertEqual(matrix["a"]["grade"]["raw_score"], .2)
        self.assertEqual(matrix["b"]["grade"]["raw_score"], .8)
        self.assertEqual(exact["b"]["exact"]["raw_score"], 1)
        with self.assertRaisesRegex(PhotographyError, "same scoring policy"):
            self.query({"id": "grade", **base}, {"id": "exact", **base, "scoring": "exact"})
        self.assertEqual(matrix["c"]["grade"]["status"], "not_matched")
        for changes in ({"scene_id": "unknown"}, {"minimum": None}, {"minimum": -1.01}, {"minimum": True}):
            with self.subTest(changes=changes), self.assertRaises(PhotographyError):
                normalize_query({"conditions": [{"id": "S", **base, **changes}]}, store=self.store)
        self.photo("a", "changed")
        matrix = self.query({"id": "S", **base})
        self.assertEqual(matrix["a"]["S"]["status"], "unknown_stale")

    def test_duplicates_exact_need_no_feature_profile_or_thumbnail_and_scope_is_hard(self):
        condition = {"id": "D", "kind": "has_near_duplicate", "metric": "exact"}
        with patch.object(self.store, "thumbnail", side_effect=AssertionError("Exact comparison has no preview")):
            matrix = self.query(condition)
        self.assertEqual(matrix["a"]["D"]["status"], "matched")
        self.assertEqual(matrix["b"]["D"]["evidence"], {"best_distance": 0, "peer_id": "a", "peer_count": 1})
        self.assertEqual([source["photo_id"] for source in matrix["a"]["D"]["sources"]], ["a", "b"])
        self.assertEqual(matrix["c"]["D"]["status"], "not_matched")
        self.assertEqual(self.query(condition, ids=["a"])["a"]["D"]["status"], "not_matched")
        normalized = normalize_query({"conditions": [condition]}, store=self.store)["query"]["conditions"][0]
        self.assertIsNone(normalized["profile_id"])
        self.assertEqual(normalized["max_distance"], 0)
        for changes in ({"max_distance": 1}, {"profile_id": "not-used"}):
            with self.assertRaises(PhotographyError):
                normalize_query({"conditions": [{**condition, **changes}]}, store=self.store)

    def test_hamming_witness_counts_best_distance_and_nontransitive_edges(self):
        self.hash("a", 0)
        self.hash("b", 1)
        self.hash("c", 3)
        condition = {"id": "D", "kind": "has_near_duplicate", "metric": "hamming", "max_distance": 1}
        matrix = self.query(condition)
        self.assertEqual(matrix["a"]["D"]["evidence"], {"best_distance": 1, "peer_id": "b", "peer_count": 1})
        self.assertEqual(matrix["b"]["D"]["evidence"], {"best_distance": 1, "peer_id": "a", "peer_count": 2})
        self.assertEqual(matrix["c"]["D"]["evidence"], {"best_distance": 1, "peer_id": "b", "peer_count": 1})
        self.assertEqual([source["photo_id"] for source in matrix["a"]["D"]["sources"]], ["a", "b"])
        scoped = self.query(condition, ids=["a", "c"])
        self.assertTrue(all(row["D"]["status"] == "not_matched" for row in scoped.values()))
        for distance in (None, -1, 65, .5, True):
            with self.assertRaises(PhotographyError):
                normalize_query({"conditions": [{**condition, "max_distance": distance}]}, store=self.store)

    def test_hamming_missing_stale_incomplete_peers_never_prove_absence(self):
        self.hash("a", 0)
        condition = {"id": "D", "kind": "has_near_duplicate", "metric": "hamming", "max_distance": 0}
        matrix = self.query(condition)
        self.assertEqual(matrix["a"]["D"]["status"], "unknown_missing_index")
        self.assertEqual(matrix["b"]["D"]["status"], "unknown_missing_index")
        self.hash("b", 0)
        matrix = self.query(condition)
        self.assertEqual(matrix["a"]["D"]["status"], "matched")
        self.assertEqual(matrix["b"]["D"]["status"], "matched")
        self.hash("c", 0, complete=False)
        self.photo("b", "changed")
        matrix = self.query(condition)
        self.assertEqual(matrix["b"]["D"]["status"], "unknown_stale")
        self.assertEqual(matrix["c"]["D"]["status"], "unknown_invalid")
        self.assertNotEqual(matrix["a"]["D"]["status"], "not_matched")

    def test_virtual_folder_scope_cannot_import_an_outside_duplicate_peer(self):
        folder = create_folder("scope", store=self.store)["folder"]["folder_id"]
        add_photos(folder, ["a", "c"], store=self.store)
        raw = {"scope": {"folder_ids": [folder]}, "conditions": [
            {"id": "D", "kind": "has_near_duplicate", "metric": "exact"}]}
        with self.readonly():
            query = normalize_query(raw, store=self.store)["query"]
            _, photos = resolve_scope(store=self.store, folder_ids=query["scope"]["folder_ids"],
                                      folder_match=query["scope"]["match"])
            matrix = feature_predicates.evaluate_conditions(query["conditions"], photos, store=self.store)
        self.assertEqual(set(matrix), {"a", "c"})
        self.assertTrue(all(row["D"]["status"] == "not_matched" for row in matrix.values()))

    def test_result_corruption_is_unknown_not_false_and_invalid_ingest_is_independent(self):
        self.objects("a")
        self.objects("b")
        self.store.db.execute("UPDATE photos SET ingest_state='error' WHERE photo_id='b'")
        condition = {"id": "N", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 0}
        with patch.object(self.store, "find_feature_result",
                          side_effect=PhotographyError("FEATURE_RESULT_INVALID", "corrupt")):
            matrix = self.query(condition)
        self.assertEqual(matrix["a"]["N"]["status"], "unknown_invalid")
        self.assertEqual(matrix["b"]["N"]["status"], "unknown_invalid")

    def test_cell_whitelist_is_detached_and_rejects_payloads_wrong_types_or_unknown_scores(self):
        self.objects("a")
        condition = normalize_query({"conditions": [
            {"id": "N", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 0}]},
            store=self.store)["query"]["conditions"][0]
        cell = self.query(condition)["a"]["N"]
        saved = deepcopy(cell)
        detached = validate_cell(condition, cell)
        detached["sources"][0]["photo_id"] = "changed"
        detached["evidence"]["count"] = 20
        self.assertEqual(cell, saved)
        mutations = [
            {"pixels": "bad"}, {"raw_score": True}, {"raw_score": .8}, {"direction": "lower"},
            {"score_type": "cosine"}, {"status": "not_reviewed"}, {"evidence": {"count": True}},
            {"evidence": {"payload": {}}}, {"sources": {}}, {"sources": [{"kind": "feature"}]},
            {"sources": [{**cell["sources"][0], "payload": {}}]},
            {"sources": [{**cell["sources"][0], "result_id": 1}]},
            {"status": "unknown_missing_index", "raw_score": 1}, {"reason": {}},
            {"normalized_score": 1},
        ]
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(PhotographyError):
                validate_cell(condition, {**cell, **changes})
        final = validate_cell(condition, {**cell, "normalized_score": 1}, finalized=True)
        self.assertEqual(final["normalized_score"], 1)
        with self.assertRaises(PhotographyError):
            validate_cell(condition, {**cell, "normalized_score": .5}, finalized=True)

    def test_semantic_cells_allow_pending_raw_scores_but_not_final_pending_or_arbitrary_evidence(self):
        condition = {"id": "S", "kind": "semantic", "scoring": "graded"}
        cell = {"status": "not_reviewed", "raw_score": .25, "score_type": "cosine", "direction": "higher",
                "sources": [], "evidence": {"candidate_rank": 1, "score_gap_from_best": 0,
                                            "score_gap_to_next": None}, "reason": None}
        validate_cell(condition, cell)
        with self.assertRaises(PhotographyError):
            validate_cell(condition, cell, finalized=True)
        for score in (float("nan"), float("inf"), 1.1, True):
            with self.assertRaises(PhotographyError):
                validate_cell(condition, {**cell, "raw_score": score})

    def test_readonly_reopened_database_preserves_results_without_originals(self):
        self.ocr("a", "中国人")
        self.hash("a", 0)
        self.hash("b", 0)
        self.store.close()
        self.store = SQLiteStorage.open(self.path)
        matrix = self.query({"id": "T", "kind": "ocr_contains", "text": "中国人"},
                            {"id": "D", "kind": "has_near_duplicate", "metric": "hamming", "max_distance": 0})
        self.assertEqual(matrix["a"]["T"]["status"], "matched")
        self.assertEqual(matrix["a"]["D"]["status"], "matched")


if __name__ == "__main__":
    unittest.main()
