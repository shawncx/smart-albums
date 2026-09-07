"""Feature persistence tests: synthetic rows only, no images, models or downloads."""
from __future__ import annotations

import copy
import hashlib
import shutil
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from photography_lib.config import PhotographyError
from photography_lib import image_feature_storage as feature_storage_module
from photography_lib.feature_profiles import default_profile, normalize_ocr_text
from photography_lib.fingerprints import fingerprint
from photography_lib.image_feature_storage import FEATURE_ALL_TABLES, IMAGE_FEATURE_SCHEMA
from photography_lib.image_vectors import pack_vector
from photography_lib.sqlite_storage import SQLiteStorage


EMBEDDING_PROFILE = {
    "profile_schema": "image-embedding-profile-v1", "embedding_kind": "image_text_semantic",
    "stored_modality": "image", "input_scope": "stored_thumbnail", "granularity": "whole_image",
    "model": "synthetic", "revision": "fixture-v1", "dimensions": 3,
    "dtype": "float32-le", "normalized": True,
}


class FeatureStorageTests(unittest.TestCase):
    def setUp(self):
        self.base = PROJECT / (".feature-storage-tests-" + uuid4().hex)
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base)
        self.path = self.base / "features.sqlite"
        self.store = SQLiteStorage.create(self.path)
        self.addCleanup(lambda: self.store.close())
        for photo_id in ("a", "b", "c"):
            self.photo(photo_id)

    def photo(self, photo_id, content=None):
        stamp = "2026-09-06T00:00:00+00:00"
        photo = {
            "photo_id": photo_id, "original_absolute_path": str(self.base / "missing" / (photo_id + ".jpg")),
            "content_version": content or hashlib.sha256(b"same synthetic content").hexdigest(),
            "thumbnail_profile": "synthetic-preview-v1", "size_bytes": 17, "mtime_ns": 1,
            "metadata": {"width": 10, "height": 5, "display_width": 10, "display_height": 5},
            "ingest_state": "available", "original_status": "not_checked", "created_at": stamp, "updated_at": stamp,
        }
        self.store.put_photo(photo)
        self.store.db.execute("""INSERT OR REPLACE INTO thumbnails VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (photo_id, photo["content_version"], photo["thumbnail_profile"], hashlib.sha256(b"not an image").hexdigest(),
             "image/jpeg", 10, 5, stamp, 12, b"not an image"))
        return photo

    def profile(self, component, dependency=None):
        profile = default_profile(component, dependency_profile_id=dependency)
        return self.store.put_feature_profile(profile)

    def manifest(self, profile_id, photo_id="a", **extra):
        profile = self.store.feature_profile(profile_id)
        photo = self.store.photo(photo_id)
        result = {"content_version": photo["content_version"], "input_scope": profile["input_scope"]}
        if profile["input_scope"] == "original":
            result.update(original_hash=photo["content_version"], width=10, height=5)
        elif profile["input_scope"] == "stored_thumbnail":
            thumbnail = self.store.thumbnail(photo_id, include_data=False)
            result.update(thumbnail_profile=thumbnail["profile"], input_image_hash=thumbnail["image_hash"], width=10, height=5)
        result.update(extra)
        return result

    @staticmethod
    def payload(component, **changes):
        values = {
            "ocr": {"width": 10, "height": 5, "complete": True, "text": "", "normalized_text": "", "blocks": []},
            "objects": {"width": 10, "height": 5, "complete": True, "objects": []},
            "color": {"width": 10, "height": 5, "complete": True,
                      "features": {"mean_saturation": 0.5, "low_saturation_fraction": 0.2,
                                   "hue_histogram": [1, *([0] * 11)]},
                      "palette": [{"rgb": [0, 0, 255], "fraction": 1}]},
            "composition": {"width": 10, "height": 5, "complete": True,
                            "features": {"subject_index": None, "center_x": None, "center_y": None,
                                         "subject_area": None, "thirds_distance": None, "union_area": 0,
                                         "bounding_area": 0, "uncovered_fraction": 1}},
            "perceptual_hash": {"complete": True, "algorithm": "dhash", "bits": 64, "hash_hex": "0000000000000000"},
        }
        result = values[component]
        result.update(changes)
        return result

    def put(self, component, photo_id="a", **changes):
        profile_id = self.profile(component)
        payload = self.payload(component, **changes)
        result_id = self.store.put_feature_result(photo_id, profile_id, self.manifest(profile_id, photo_id), payload)
        return profile_id, result_id

    def plan(self, profile_id, *, kind="extract", photo_id="a", manifest=None, action="compute", result_id=None, options=None):
        manifest = manifest if manifest is not None else self.manifest(profile_id, photo_id)
        profile = self.store.feature_profile(profile_id)
        item = {"item_id": photo_id if kind == "extract" else kind, "photo_id": photo_id if kind == "extract" else None,
                "manifest": manifest, "input_fingerprint": fingerprint(manifest), "action": action,
                "result_id": result_id, "reason": None}
        plan = {"schema": "image-feature-plan-v1", "run_id": "feature_" + uuid4().hex,
                "work_kind": kind, "album_id": self.store.album()["id"], "component": profile["component"],
                "profile_id": profile_id, "profile": profile, "created_at": "2026-09-06T00:00:00+00:00",
                "options": options or {}, "items": [item],
                "counts": {"total": 1, "compute": int(action == "compute"), "reuse": int(action == "reuse"),
                           "skip": int(action == "skip")}}
        plan["digest"] = fingerprint(plan)
        self.store.create_feature_run(plan)
        return plan

    def scene(self):
        embedding_id = self.store.put_embedding_profile(EMBEDDING_PROFILE)
        profile_id = self.profile("scene", embedding_id)
        profile = self.store.feature_profile(profile_id)
        prototypes = [{**scene, "vector": [1.0, 0.0, 0.0]} for scene in profile["parameters"]["catalog"]]
        set_id = self.store.put_scene_prototypes(profile_id, prototypes)
        blob = pack_vector([1, 0, 0], 3)
        thumbnail = self.store.thumbnail("a", include_data=False)
        snapshot = {"photo_id": "a", "content_version": self.store.photo("a")["content_version"],
                    "thumbnail_profile": thumbnail["profile"], "input_image_hash": thumbnail["image_hash"]}
        source_id = self.store._put_embedding_result(snapshot, embedding_id, blob, hashlib.sha256(blob).hexdigest(), 3)
        saved_set = self.store.scene_prototype_set(profile_id)
        manifest = self.manifest(profile_id, embedding_result_id=source_id, embedding_profile_id=embedding_id,
                                 vector_hash=hashlib.sha256(blob).hexdigest(), prototype_set_id=set_id,
                                 prototype_hash=saved_set["payload_hash"])
        payload = {"complete": True, "scores": [{"scene_id": scene["scene_id"], "score": 1}
                                                for scene in profile["parameters"]["catalog"]]}
        return profile_id, manifest, payload

    def test_profile_identity_defaults_and_cross_component_foreign_keys(self):
        color = self.profile("color")
        objects = self.profile("objects")
        self.assertEqual(color, self.profile("color"))
        self.assertEqual(len(self.store.feature_profiles()), 2)
        self.assertEqual(len(self.store.feature_profiles("color")), 1)
        self.assertIsNone(self.store.default_feature_profile("color"))
        self.store.set_default_feature_profile("color", color)
        with self.assertRaises(PhotographyError):
            self.store.set_default_feature_profile("objects", color)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("INSERT INTO image_feature_settings VALUES ('objects',?)", (color,))
        self.assertEqual(self.store.default_feature_profile("color"), color)
        with self.assertRaises(PhotographyError):
            self.profile("composition", color)
        self.profile("composition", objects)
        with self.assertRaises(PhotographyError):
            self.profile("scene", "unregistered-embedding")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE image_feature_profiles SET component='objects' WHERE profile_id=?", (color,))

    def test_direct_results_reconstruct_and_preserve_numeric_json(self):
        for component in ("ocr", "objects", "color", "perceptual_hash"):
            with self.subTest(component=component):
                profile_id, result_id = self.put(component)
                result = self.store.feature_result(result_id)
                self.assertEqual(result["payload"], self.payload(component))
                self.assertEqual(result["payload_hash"], fingerprint(result["payload"]))
                self.assertEqual(result["input_fingerprint"], fingerprint(self.manifest(profile_id)))
                self.assertEqual(self.store.find_feature_result("a", profile_id, result["input_fingerprint"]), result)
                self.assertTrue(self.store.has_feature_results("a", profile_id))
                self.assertEqual(self.store.put_feature_result("a", profile_id, self.manifest(profile_id),
                                                              self.payload(component)), result_id)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_feature_results").fetchone()[0], 4)

    def test_invalid_payloads_manifests_and_write_conflicts_leave_no_partial_results(self):
        profile_id = self.profile("color")
        valid = self.manifest(profile_id)
        for bad in ({**valid, "width": 0}, {**valid, "input_scope": "original"},
                    {**valid, "content_version": "changed"}, {**valid, "pixels": "base64"},
                    {key: value for key, value in valid.items() if key != "width"}):
            with self.subTest(manifest=bad), self.assertRaises(PhotographyError):
                self.store.put_feature_result("a", profile_id, bad, self.payload("color"))
        invalid = self.payload("color")
        invalid["features"]["mean_saturation"] = float("nan")
        with self.assertRaises(PhotographyError):
            self.store.put_feature_result("a", profile_id, valid, invalid)
        with self.assertRaises(PhotographyError):
            self.store.put_feature_result("a", profile_id, valid, {**self.payload("color"), "pixels": "secret"})
        self.assertFalse(self.store.has_feature_results("a", profile_id))
        with patch.object(self.store, "_put_feature_details", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.store.put_feature_result("a", profile_id, valid, self.payload("color"))
        self.assertFalse(self.store.has_feature_results("a", profile_id))
        self.put("color")
        changed = self.payload("color")
        changed["features"]["mean_saturation"] = 0.8
        with self.assertRaises(PhotographyError):
            self.store.put_feature_result("a", profile_id, valid, changed)

    def test_results_details_and_job_success_publish_in_one_transaction(self):
        profile_id = self.profile("color")
        plan = self.plan(profile_id)
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.store.transaction():
                result_id = self.store.put_feature_result("a", profile_id, self.manifest(profile_id), self.payload("color"))
                self.store.update_feature_item(plan["run_id"], {"item_id": "a", "status": "computed", "result_id": result_id})
                raise RuntimeError("abort")
        self.assertFalse(self.store.has_feature_results("a", profile_id))
        self.assertEqual(self.store.feature_items(plan["run_id"])[0]["status"], "pending")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_color_features").fetchone()[0], 0)

    def test_wrong_typed_parent_and_missing_photo_foreign_keys(self):
        profile_id, result_id = self.put("color")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("INSERT INTO image_ocr_documents(result_id,text,normalized_text,block_count) VALUES (?,'','',0)",
                                  (result_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("INSERT INTO image_color_features(result_id,mean_saturation,low_saturation_fraction,hue_histogram_json) "
                                  "VALUES ('missing',0,0,'[]')")
        with self.assertRaises(PhotographyError):
            self.store.put_feature_result("missing", profile_id, self.manifest(profile_id), self.payload("color"))

    def test_history_is_result_id_order_and_keeps_stale_successes(self):
        profile_id, first = self.put("color")
        self.photo("a", "new-content")
        unused, second = self.put("color")
        result_ids = sorted([first, second])
        page = self.store.feature_history("a", profile_id, limit=1)
        self.assertEqual(page[0]["result_id"], result_ids[0])
        self.assertEqual(self.store.feature_history("a", profile_id, after=result_ids[0])[0]["result_id"], result_ids[1])
        self.assertNotEqual(self.store.feature_result(first)["content_version"], self.store.photo("a")["content_version"])

    def test_typed_corruption_is_detected_on_fetch_not_heavy_open_scan(self):
        unused, result_id = self.put("color")
        self.store.db.execute("DROP TRIGGER image_color_features_update_immutable")
        self.store.db.execute("UPDATE image_color_features SET mean_saturation=0.9 WHERE result_id=?", (result_id,))
        with self.assertRaises(PhotographyError):
            self.store.feature_result(result_id)
        with self.assertRaises(PhotographyError) as error:
            SQLiteStorage.open(self.path)
        self.assertEqual(error.exception.code, "SCHEMA_INVALID")
        statement = next(sql for sql in IMAGE_FEATURE_SCHEMA
                         if sql.startswith("CREATE TRIGGER image_color_features_update_immutable "))
        self.store.db.execute(statement)
        with SQLiteStorage.open(self.path) as store:
            with self.assertRaises(PhotographyError) as failure:
                store.feature_result(result_id)
        self.assertEqual(failure.exception.code, "FEATURE_RESULT_INVALID")

    def test_missing_typed_bundle_and_payload_hash_corruption_are_rejected(self):
        unused, result_id = self.put("color")
        self.store.db.execute("DROP TRIGGER image_color_features_delete_immutable")
        self.store.db.execute("DELETE FROM image_color_features WHERE result_id=?", (result_id,))
        with self.assertRaises(PhotographyError):
            self.store.feature_result(result_id)
        unused, result_id = self.put("objects")
        self.store.db.execute("DROP TRIGGER image_feature_results_update_immutable")
        self.store.db.execute("UPDATE image_feature_results SET payload_hash='wrong' WHERE result_id=?", (result_id,))
        with self.assertRaises(PhotographyError):
            self.store.feature_result(result_id)

    def test_bad_hash_blob_and_extra_audit_metadata_never_escape_validation(self):
        unused, result_id = self.put("perceptual_hash")
        self.store.db.execute("DROP TRIGGER image_perceptual_hashes_update_immutable")
        self.store.db.execute("UPDATE image_perceptual_hashes SET hash='12345678' WHERE result_id=?", (result_id,))
        with self.assertRaises(PhotographyError) as failure:
            self.store.feature_result(result_id)
        self.assertEqual(failure.exception.code, "FEATURE_RESULT_INVALID")
        unused, result_id = self.put("objects")
        self.store.db.execute("DROP TRIGGER image_feature_results_update_immutable")
        self.store.db.execute("""UPDATE image_feature_results
            SET payload_json=json_set(payload_json,'$.pixels','must not be returned') WHERE result_id=?""", (result_id,))
        with self.assertRaises(PhotographyError):
            self.store.feature_result(result_id)
    def test_composition_dependencies_are_typed_same_photo_and_hash_bound(self):
        objects, source_id = self.put("objects")
        profile_id = self.profile("composition", objects)
        source = self.store.feature_result(source_id)
        manifest = self.manifest(profile_id, source_result_id=source_id, source_profile_id=objects,
                                 source_payload_hash=source["payload_hash"], width=10, height=5)
        for photo_id, dependencies, bad_manifest in (
            ("a", [], manifest), ("b", [source_id], manifest),
            ("a", [source_id], {**manifest, "source_payload_hash": "wrong"}),
            ("a", [source_id], {**manifest, "source_profile_id": self.profile("color")})):
            with self.subTest(photo_id=photo_id, dependencies=dependencies), self.assertRaises(PhotographyError):
                self.store.put_feature_result(photo_id, profile_id, bad_manifest, self.payload("composition"),
                                              feature_dependencies=dependencies)
        result_id = self.store.put_feature_result("a", profile_id, manifest, self.payload("composition"),
                                                  feature_dependencies=[source_id])
        self.assertEqual(self.store.feature_result(result_id)["feature_dependencies"], [source_id])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("""INSERT INTO image_feature_dependencies
                (result_id,photo_id,source_result_id,source_profile_id,content_version,source_payload_hash)
                VALUES (?,?,?,?,?,?)""", (source_id, "a", result_id, profile_id, manifest["content_version"], source["payload_hash"]))

    def test_scene_prototypes_and_embedding_dependency_identity(self):
        profile_id, manifest, payload = self.scene()
        result_id = self.store.put_feature_result("a", profile_id, manifest, payload,
                                                  embedding_dependencies=[manifest["embedding_result_id"]])
        self.assertEqual(self.store.feature_result(result_id)["payload"], payload)
        self.assertNotIn("width", self.store.feature_result(result_id)["payload"])
        saved = self.store.scene_prototype_set(profile_id)
        self.assertEqual(self.store.put_scene_prototypes(profile_id, saved["prototypes"]), saved["set_id"])
        for change in ({"vector": [2, 0, 0]}, {"vector": [float("nan"), 0, 0]},
                       {"prompts": ["unapproved prompt"]}, {"vector": [0, 1, 0]}):
            invalid = copy.deepcopy(saved["prototypes"])
            invalid[0].update(change)
            with self.subTest(change=change), self.assertRaises(PhotographyError):
                self.store.put_scene_prototypes(profile_id, invalid)
        with self.assertRaises(PhotographyError):
            self.store.put_feature_result("b", profile_id, manifest, payload,
                                          embedding_dependencies=[manifest["embedding_result_id"]])
        with self.assertRaises(PhotographyError):
            self.store.put_feature_result("a", profile_id, {**manifest, "prototype_hash": "wrong"}, payload,
                                          embedding_dependencies=[manifest["embedding_result_id"]])
        self.store.db.execute("UPDATE image_embedding_results SET vector_hash='corrupt' WHERE result_id=?",
                              (manifest["embedding_result_id"],))
        with self.assertRaises(PhotographyError):
            self.store.feature_result(result_id)

    def test_ocr_fts_chinese_short_words_empty_documents_and_explicit_rebuild(self):
        text = "蓝色海边照片 Cat"
        payload = self.payload("ocr", text=text, normalized_text=normalize_ocr_text(text),
                               blocks=[{"text": text, "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]],
                                        "recognition_score": None, "detection_score": None}])
        profile_id = self.profile("ocr")
        result_id = self.store.put_feature_result("a", profile_id, self.manifest(profile_id), payload)
        self.assertEqual(self.store.feature_result(result_id)["payload"], payload)
        self.put("ocr", "b")
        matches = lambda term: self.store.db.execute("SELECT rowid FROM image_ocr_fts WHERE image_ocr_fts MATCH ?", (term,)).fetchall()
        self.assertEqual(len(matches("海边照")), 1)
        self.assertEqual(len(matches("cat")), 1)
        self.assertEqual(len(matches("海边")), 0)
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) FROM image_ocr_documents WHERE instr(normalized_text,'海边')>0").fetchone()[0], 1)
        self.store.db.execute("INSERT INTO image_ocr_fts(image_ocr_fts) VALUES('delete-all')")
        self.assertEqual(matches("海边照"), [])
        self.assertEqual(self.store.rebuild_feature_fts(), {"documents": 2, "rebuilt": True})
        self.assertEqual(len(matches("海边照")), 1)
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.put("ocr", "c", **{key: value for key, value in payload.items() if key not in ("width", "height", "complete")})
                raise RuntimeError("rollback FTS")
        self.assertEqual(len(matches("海边照")), 1)

    def test_run_identity_mutable_progress_claims_and_result_type(self):
        profile_id, result_id = self.put("color")
        plan = self.plan(profile_id)
        run_id = plan["run_id"]
        self.assertEqual(self.store.feature_run(run_id)["status"], "proposed")
        self.store.update_feature_run(run_id, confirmed_digest=plan["digest"], confirmed_at=plan["created_at"], model_calls=1)
        with self.assertRaises(PhotographyError):
            self.store.update_feature_run(run_id, confirmed_digest="wrong")
        with self.assertRaises(PhotographyError):
            self.store.update_feature_run(run_id, model_calls=0)
        with self.assertRaises(PhotographyError):
            self.store.update_feature_run(run_id, plan={})
        with self.assertRaises(PhotographyError):
            self.store.claim_feature_input(run_id, "a", profile_id, "wrong")
        self.store.claim_feature_input(run_id, "a", profile_id, plan["items"][0]["input_fingerprint"])
        another = self.plan(profile_id)
        with self.assertRaises(PhotographyError):
            self.store.claim_feature_input(another["run_id"], "a", profile_id, plan["items"][0]["input_fingerprint"])
        self.store.release_feature_input(run_id, "a")
        self.store.claim_feature_input(another["run_id"], "a", profile_id, plan["items"][0]["input_fingerprint"])
        self.store.update_feature_item(run_id, {"item_id": "a", "status": "computed", "result_id": result_id})
        self.assertEqual(self.store.feature_items(run_id)[0]["snapshot"], plan["items"][0])
        unused, other_id = self.put("color", "b")
        with self.assertRaises(PhotographyError):
            self.store.update_feature_item(run_id, {"item_id": "a", "result_id": other_id})
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE image_feature_items SET result_id=? WHERE run_id=?", (other_id, run_id))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE image_feature_runs SET digest='changed' WHERE run_id=?", (run_id,))
        with self.assertRaises(PhotographyError):
            self.store.update_feature_item(run_id, {"item_id": "a", "snapshot": {}})
        with self.assertRaises(PhotographyError):
            self.store.update_feature_item(run_id, {"item_id": "a", "status": "succeeded"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE image_feature_items SET status='succeeded' WHERE run_id=?", (run_id,))

    def test_hot_batch_updates_claims_and_counters_have_linear_storage_work(self):
        size = 300
        profile_id = self.profile("color")
        plan = self.plan(profile_id)
        plan.update(run_id="feature_" + uuid4().hex, items=[],
                    counts={"total": size, "compute": size, "reuse": 0, "skip": 0})
        results = {}
        with self.store.transaction():
            for index in range(size):
                photo_id = f"batch_{index:03}"
                self.photo(photo_id)
                manifest = self.manifest(profile_id, photo_id)
                results[photo_id] = self.store.put_feature_result(photo_id, profile_id, manifest, self.payload("color"))
                plan["items"].append({"item_id": photo_id, "photo_id": photo_id, "manifest": manifest,
                                      "input_fingerprint": fingerprint(manifest), "action": "compute",
                                      "result_id": None, "reason": None})
            plan["digest"] = fingerprint({key: value for key, value in plan.items() if key != "digest"})
            self.store.create_feature_run(plan)
        with patch.object(self.store, "feature_run", side_effect=AssertionError("whole-plan hot read")), \
             patch.object(self.store, "feature_items", side_effect=AssertionError("whole-run hot walk")), \
             patch.object(self.store, "_target_feature_item", wraps=self.store._target_feature_item) as targets, \
             patch.object(self.store, "feature_result", wraps=self.store.feature_result) as reconstructed, \
             patch.object(feature_storage_module, "_load", wraps=feature_storage_module._load) as decoded:
            with self.store.transaction():
                for index, snapshot in enumerate(plan["items"]):
                    item_id = snapshot["item_id"]
                    self.store.claim_feature_input(plan["run_id"], item_id, profile_id, snapshot["input_fingerprint"])
                    self.store.update_feature_item(plan["run_id"], {
                        "item_id": item_id, "status": "computed", "result_id": results[item_id]})
                    self.store.update_feature_run(plan["run_id"], model_calls=index + 1)
                    self.store.release_feature_input(plan["run_id"], item_id)
            self.assertEqual(targets.call_count, 2 * size)
            self.assertEqual(reconstructed.call_count, size)
            self.assertLessEqual(decoded.call_count, 20 * size)
            self.assertFalse(any('"schema":"image-feature-plan-v1"' in call.args[0] for call in decoded.call_args_list))
        self.assertEqual(self.store.feature_run(plan["run_id"])["model_calls"], size)
        self.assertEqual([item["result_id"] for item in self.store.feature_items(plan["run_id"])],
                         [results[snapshot["item_id"]] for snapshot in plan["items"]])

    def test_targeted_updates_and_claims_reject_tampered_item_identity(self):
        profile_id, unused = self.put("color")
        plan = self.plan(profile_id)
        run_id = plan["run_id"]
        snapshot_json = self.store.db.execute(
            "SELECT snapshot_json FROM image_feature_items WHERE run_id=?", (run_id,)).fetchone()[0]
        self.store.db.execute("DROP TRIGGER image_feature_snapshot_immutable")
        for field, value in (("$.photo_id", "b"), ("$.manifest.width", 11), ("$.input_fingerprint", "wrong")):
            self.store.db.execute("UPDATE image_feature_items SET snapshot_json=json_set(?,?,?) WHERE run_id=?",
                                  (snapshot_json, field, value, run_id))
            for operation in (
                lambda: self.store.update_feature_item(run_id, {"item_id": "a", "status": "running"}),
                lambda: self.store.claim_feature_input(run_id, "a", profile_id, plan["items"][0]["input_fingerprint"]),
            ):
                with self.subTest(field=field), self.assertRaises(PhotographyError):
                    operation()
        self.store.db.execute("UPDATE image_feature_items SET snapshot_json=? WHERE run_id=?", (snapshot_json, run_id))
        unused, wrong_result = self.put("color", "b")
        self.store.db.execute("DROP TRIGGER image_feature_item_result_update")
        self.store.db.execute("UPDATE image_feature_items SET result_id=? WHERE run_id=?", (wrong_result, run_id))
        with self.assertRaises(PhotographyError):
            self.store.update_feature_item(run_id, {"item_id": "a", "status": "running", "result_id": None})
        with self.assertRaises(PhotographyError):
            self.store.claim_feature_input(run_id, "a", profile_id, plan["items"][0]["input_fingerprint"])

    def test_public_job_reads_still_reject_full_plan_tampering(self):
        profile_id = self.profile("color")
        plan = self.plan(profile_id)
        self.store.db.execute("DROP TRIGGER image_feature_plan_immutable")
        self.store.db.execute("""UPDATE image_feature_runs
            SET plan_json=json_set(plan_json,'$.counts.total',42) WHERE run_id=?""", (plan["run_id"],))
        for read in (self.store.feature_run, self.store.feature_items):
            with self.subTest(read=read), self.assertRaises(PhotographyError) as failure:
                read(plan["run_id"])
            self.assertEqual(failure.exception.code, "FEATURE_PLAN_INVALID")

    def test_prototype_jobs_reference_only_their_profile_set(self):
        profile_id, unused, unused_payload = self.scene()
        saved = self.store.scene_prototype_set(profile_id)
        profile = self.store.feature_profile(profile_id)
        manifest = {"embedding_profile_id": profile["dependencies"]["image_embedding"],
                    "embedding_profile_hash": profile["dependencies"]["image_embedding"],
                    "catalog": profile["parameters"]["catalog"]}
        plan = self.plan(profile_id, kind="prototypes", manifest=manifest)
        self.store.update_feature_item(plan["run_id"], {"item_id": "prototypes", "status": "computed", "result_id": saved["set_id"]})
        unused, wrong_result = self.put("color")
        with self.assertRaises(PhotographyError):
            self.store.update_feature_item(plan["run_id"], {"item_id": "prototypes", "result_id": wrong_result})
        self.assertIsNone(self.store.feature_items(plan["run_id"])[0]["photo_id"])

    def compare(self, metric="hamming"):
        profile_id = self.profile("perceptual_hash")
        participants = []
        for photo_id in ("a", "b", "c"):
            result_id = None
            if metric == "hamming":
                unused, result_id = self.put("perceptual_hash", photo_id, hash_hex="000000000000000" + str(ord(photo_id) - 97))
            participants.append({"photo_id": photo_id, "content_version": self.store.photo(photo_id)["content_version"], "result_id": result_id})
        options = {"metric": metric, "max_distance": 64 if metric == "hamming" else 0}
        plan = self.plan(profile_id, kind="compare", manifest={"participants": participants, **options}, options=options)
        return plan, participants

    @staticmethod
    def pair(a, b, metric="hamming", distance=1):
        return {"photo_id_a": a["photo_id"], "photo_id_b": b["photo_id"], "result_id_a": a["result_id"],
                "result_id_b": b["result_id"], "content_version_a": a["content_version"],
                "content_version_b": b["content_version"], "metric": metric, "distance": distance}

    def test_compare_pair_sorting_idempotence_pagination_and_history(self):
        plan, participants = self.compare()
        a, b, c = participants
        self.store.put_similarity_pairs(plan["run_id"], [self.pair(b, a), self.pair(a, c), self.pair(b, c, distance=2)])
        self.store.put_similarity_pairs(plan["run_id"], [self.pair(a, b)])
        first = self.store.similarity_pairs(plan["run_id"], limit=2)
        self.assertEqual((len(first["items"]), first["total"]), (2, 3))
        self.assertEqual(first["items"][0]["photo_id_a"], "a")
        last = self.store.similarity_pairs(plan["run_id"], limit=2, after=first["next_cursor"])
        self.assertEqual(len(last["items"]), 1)
        self.assertIsNone(last["next_cursor"])
        self.photo("a", "modified")
        self.assertEqual(self.store.similarity_pairs(plan["run_id"])["total"], 3)
        with self.assertRaises(PhotographyError):
            self.store.put_similarity_pairs(plan["run_id"], [self.pair(a, b)])

    def test_compare_rejects_wrong_scope_distance_identity_and_atomic_batch(self):
        plan, participants = self.compare()
        a, b, c = participants
        for pair in (self.pair(a, a), self.pair(a, b, distance=7), self.pair(a, b, metric="exact"),
                     self.pair(a, b, distance=float("nan")), {**self.pair(a, b), "photo_id_b": "outside"},
                     {**self.pair(a, b), "result_id_b": c["result_id"]}):
            with self.subTest(pair=pair), self.assertRaises(PhotographyError):
                self.store.put_similarity_pairs(plan["run_id"], [pair])
        with self.assertRaises(PhotographyError):
            self.store.put_similarity_pairs(plan["run_id"], [self.pair(a, b), self.pair(a, a)])
        self.assertEqual(self.store.similarity_pairs(plan["run_id"])["total"], 0)
        with self.assertRaises(PhotographyError):
            self.store.update_feature_item(plan["run_id"], {"item_id": "compare", "result_id": a["result_id"]})

    def test_exact_comparison_uses_ingested_content_without_feature_results(self):
        plan, participants = self.compare("exact")
        self.store.put_similarity_pairs(plan["run_id"], [self.pair(participants[0], participants[1], "exact", 0)])
        self.assertEqual(self.store.similarity_pairs(plan["run_id"])["total"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_feature_results").fetchone()[0], 0)
        self.store.update_feature_item(plan["run_id"], {"item_id": "compare", "status": "computed", "progress": {"comparisons": 3}})
        self.assertEqual(self.store.feature_items(plan["run_id"])[0]["progress"], {"comparisons": 3})
        for progress in ({"comparisons": -1}, {"comparisons": 4}, {"comparisons": True}, {"pixels": "not allowed"}):
            with self.subTest(progress=progress), self.assertRaises(PhotographyError):
                self.store.update_feature_item(plan["run_id"], {"item_id": "compare", "progress": progress})

    def test_strict_fts_registration_rejects_unknown_shadow_and_modified_triggers(self):
        for mutation in ("CREATE TABLE arbitrary_data(id)", "CREATE TABLE sqliteevil(id)",
                         "CREATE TABLE image_ocr_fts_content(id)",
                         "DROP TRIGGER image_ocr_fts_insert",
                         "ALTER TABLE image_color_features ADD COLUMN pixels TEXT",
                         "CREATE TRIGGER rogue_trigger AFTER INSERT ON photos BEGIN SELECT 1; END",
                         "CREATE TRIGGER unrelated_feature_trigger AFTER INSERT ON image_ocr_documents BEGIN SELECT 1; END",
                         "DROP TABLE image_ocr_fts; CREATE VIRTUAL TABLE image_ocr_fts USING fts5(normalized_text,"
                         "content='image_ocr_documents',content_rowid='document_id',tokenize='unicode61')"):
            path = self.base / (uuid4().hex + ".sqlite")
            with SQLiteStorage.create(path) as store:
                store.db.executescript(mutation)
            before = path.read_bytes()
            with self.subTest(mutation=mutation), self.assertRaises(PhotographyError) as error:
                SQLiteStorage.open(path)
            self.assertEqual(error.exception.code, "SCHEMA_INVALID")
            self.assertEqual(path.read_bytes(), before)

    def test_schema_validation_and_backup_do_not_decode_all_payloads_or_model_vectors(self):
        unused, result_id = self.put("color")
        self.scene()
        backup = self.base / "copy.sqlite"
        self.store.backup(backup)
        moved = self.base / "moved.sqlite"
        backup.rename(moved)
        with patch.object(SQLiteStorage, "feature_result", side_effect=AssertionError("per-open payload scan")), \
             patch("photography_lib.image_feature_storage.unpack_vector", side_effect=AssertionError("per-open vector scan")):
            with SQLiteStorage.open(moved) as store:
                self.assertEqual(store.album()["id"], self.store.album()["id"])
                self.assertTrue(set(FEATURE_ALL_TABLES) <= {row[0] for row in store.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")})
        before = moved.read_bytes()
        with SQLiteStorage.open(moved) as store:
            self.assertEqual(store.feature_result(result_id)["payload"], self.payload("color"))
            with self.assertRaises(PhotographyError):
                store.rebuild_feature_fts()
        self.assertEqual(moved.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
