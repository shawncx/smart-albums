"""Stage-one integration using temporary albums, real algorithms and fake vision/text providers."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from photography_lib import feature_index, feature_inputs, management
from photography_lib.cli import main
from photography_lib.config import Config, PhotographyError
from photography_lib.feature_profiles import default_profile
from photography_lib.fingerprints import fingerprint
from photography_lib.image_vectors import pack_vector
from photography_lib.sqlite_storage import SQLiteStorage


EMBEDDING = {
    "profile_schema": "image-embedding-profile-v1", "embedding_kind": "image_text_semantic",
    "stored_modality": "image", "input_scope": "stored_thumbnail", "granularity": "whole_image",
    "model": "synthetic-feature-dependency", "dimensions": 3, "dtype": "float32-le", "normalized": True,
}


class TextEncoder:
    def __init__(self):
        self.calls = 0

    def profile(self):
        return deepcopy(EMBEDDING)

    def check_ready(self):
        return {"ready": True}

    def encode_text(self, text):
        self.calls += 1
        return argparse.Namespace(vector=(1., 0., 0.), elapsed_seconds=0.001)


class Vision:
    def __init__(self, profile, callback=None):
        self.identity, self.callback = deepcopy(profile), callback
        self.calls = 0
        self.ready_checks = 0

    def profile(self):
        return deepcopy(self.identity)

    def check_ready(self):
        self.ready_checks += 1
        return {"ready": True}

    def compute(self, data):
        self.calls += 1
        if self.callback:
            self.callback()
        if self.identity["component"] == "objects":
            return {"width": 32, "height": 24, "complete": True, "objects": [
                {"class_id": "person", "score": .9, "bbox": [.1, .1, .4, .8]},
                {"class_id": "person", "score": .8, "bbox": [.6, .1, .9, .8]}]}
        return {"width": 32, "height": 24, "complete": True, "text": "HELLO",
                "normalized_text": "hello", "blocks": [
                    {"text": "HELLO", "polygon": [[.1, .1], [.9, .1], [.9, .8], [.1, .8]],
                     "recognition_score": .95, "detection_score": None}]}


class FeatureIndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="feature-index-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.source = self.base / "originals"
        self.source.mkdir()
        self.config = Config(self.base / "album.sqlite", model_cache_root=self.base / "models")
        self.store = SQLiteStorage.create(self.config.database_path)
        self.addCleanup(self.store.close)
        self.ids = ["p01", "p02", "p03"]
        self.data = self.jpeg()
        with self.store.transaction():
            for pid in self.ids:
                path = self.source / (pid + ".jpg")
                path.write_bytes(self.data)
                photo = {
                    "photo_id": pid, "content_version": hashlib.sha256(self.data).hexdigest(),
                    "thumbnail_profile": self.config.thumbnail_profile,
                    "original_absolute_path": str(path), "original_relative_path": "originals/" + path.name,
                    "size_bytes": len(self.data), "mtime_ns": path.stat().st_mtime_ns,
                    "metadata": {"display_width": 32, "display_height": 24},
                    "ingest_state": "available", "original_status": "not_checked",
                    "created_at": "2026-09-06T00:00:00Z", "updated_at": "2026-09-06T00:00:00Z",
                }
                self.store.put_photo(photo)
                self.store.put_thumbnail(photo, self.data)
        guard = patch("urllib.request.urlopen", side_effect=AssertionError("Offline fixtures may not download"))
        guard.start()
        self.addCleanup(guard.stop)

    @staticmethod
    def jpeg(color="navy"):
        data = io.BytesIO()
        Image.new("RGB", (32, 24), color).save(data, format="JPEG")
        return data.getvalue()

    def profile(self, component, dependency=None):
        profile = default_profile(component, dependency_profile_id=dependency)
        self.store.put_feature_profile(profile)
        return profile

    def plan(self, profile, ids=None, **kwargs):
        return feature_index.create_plan(ids or self.ids, store=self.store, config=self.config, profile=profile, **kwargs)

    def execute(self, plan, **kwargs):
        return feature_index.execute_plan(plan["run_id"], store=self.store, config=self.config,
                                           confirm=plan["digest"], **kwargs)

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(PhotographyError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def seed_embedding(self, pid):
        profile_id = self.store.put_embedding_profile(EMBEDDING)
        photo = self.store.photo(pid)
        snapshot = {**photo, "input_image_hash": self.store.thumbnail(pid, include_data=False)["image_hash"]}
        vector = pack_vector([1., 0., 0.], 3)
        self.store._put_embedding_result(snapshot, profile_id, vector, hashlib.sha256(vector).hexdigest(), 3)
        return profile_id

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--database", str(self.config.database_path),
                         "--model-cache-dir", str(self.config.model_cache_root), *args])
        return code, json.loads(output.getvalue())

    def test_color_and_hash_persist_current_results_and_cache_without_models(self):
        for component in ("color", "perceptual_hash"):
            with self.subTest(component=component):
                profile = self.profile(component)
                plan = self.plan(profile)
                self.assertEqual(plan["counts"]["compute"], 3)
                self.error("FEATURE_CONFIRMATION_REQUIRED", feature_index.execute_plan, plan["run_id"],
                           store=self.store, config=self.config)
                with patch("photography_lib.feature_index._vision_provider", side_effect=AssertionError("No image model")):
                    result = self.execute(plan)
                    self.assertEqual(result["status"], "completed", result)
                    self.assertEqual(result["model_calls"], 0)
                    cached = self.plan(profile)
                    self.assertEqual(cached["counts"]["reuse"], 3)
                    self.assertEqual(self.execute(cached)["model_calls_this_execution"], 0)
                current = feature_index.current_result(self.ids[0], store=self.store, profile=profile, details=True)
                self.assertEqual(current["status"], "ready")
                self.assertTrue(current["result"]["payload"]["complete"])
                self.assertEqual(len(self.store.feature_history(self.ids[0], fingerprint(profile))), 1)

    def test_ocr_originals_are_verified_but_cached_status_never_reads_them(self):
        profile = self.profile("ocr")
        provider = Vision(profile)
        result = self.execute(self.plan(profile, provider=provider), provider=provider)
        self.assertEqual((result["status"], provider.calls, result["model_calls"]), ("completed", 3, 3))
        for pid in self.ids:
            (self.source / (pid + ".jpg")).unlink()
        with patch("photography_lib.feature_inputs.resolve_original", side_effect=AssertionError("No original access")):
            current = feature_index.current_result(self.ids[0], store=self.store, profile=profile)
            self.assertEqual(current["result"]["text_length"], 5)
            self.assertNotIn("HELLO", json.dumps(current))
            detailed = feature_index.current_result(self.ids[0], store=self.store, profile=profile, details=True)
            self.assertEqual(detailed["result"]["payload"]["blocks"][0]["text"], "HELLO")
            self.assertNotIn("text", detailed["result"]["payload"])
            self.assertEqual(self.execute(self.plan(profile, provider=provider), provider=provider)["status"], "completed")
        self.assertEqual(provider.calls, 3)

    def test_missing_or_changed_original_is_not_silently_replaced_with_thumbnail(self):
        profile = self.profile("ocr")
        for mutation, state in (("missing", "input_unavailable"), ("changed", "stale")):
            with self.subTest(mutation=mutation):
                pid = self.ids[0]
                path = self.source / (pid + ".jpg")
                path.write_bytes(self.data)
                provider = Vision(profile)
                plan = self.plan(profile, [pid], provider=provider)
                path.unlink() if mutation == "missing" else path.write_bytes(self.jpeg("red"))
                result = self.execute(plan, provider=provider)
                self.assertEqual(result["items"][0]["status"], state, result)
                self.assertEqual(provider.calls, 0)
                self.assertFalse(self.store.has_feature_results(pid, fingerprint(profile)))

    def test_one_unavailable_original_does_not_block_other_ocr_photos(self):
        profile = self.profile("ocr")
        provider = Vision(profile)
        plan = self.plan(profile, provider=provider)
        (self.source / (self.ids[0] + ".jpg")).unlink()
        result = self.execute(plan, provider=provider)
        self.assertEqual(result["status"], "partial")
        self.assertEqual([item["status"] for item in result["items"]], ["input_unavailable", "computed", "computed"])
        self.assertEqual(provider.calls, 2)

    def test_input_change_during_vision_prevents_publication(self):
        profile = self.profile("objects")
        provider = Vision(profile, lambda: self.store.put_thumbnail(self.store.photo(self.ids[0]), self.jpeg("red")))
        plan = self.plan(profile, self.ids[:1], provider=provider)
        result = self.execute(plan, provider=provider)
        self.assertEqual(result["counts"]["stale"], 1)
        self.assertFalse(self.store.has_feature_results(self.ids[0], fingerprint(profile)))

    def test_provider_counters_are_typed_and_pixel_fields_are_not_persisted(self):
        profile = self.profile("objects")
        provider = Vision(profile)
        provider.last_metadata = {"onnx_calls": 1, "total_count": 2, "returned_count": 2,
                                  "truncated": False, "image": "DO_NOT_FORWARD_PIXELS"}
        result = self.execute(self.plan(profile, self.ids[:1], provider=provider), provider=provider)
        counters = result["items"][0]["attempts"][0]["provider_metadata"]
        self.assertEqual(counters["onnx_calls"], 1)
        self.assertNotIn("DO_NOT_FORWARD_PIXELS", json.dumps(result))
        provider.last_metadata["onnx_calls"] = True
        failure = self.execute(self.plan(profile, self.ids[1:2], provider=provider), provider=provider)
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["items"][0]["error"]["code"], "FEATURE_RESULT_INVALID")

    def test_reingested_content_retains_history_but_does_not_use_latest_as_current(self):
        profile = self.profile("color")
        self.execute(self.plan(profile, self.ids[:1]))
        old = feature_index.current_result(self.ids[0], store=self.store, profile=profile)["result"]
        data = self.jpeg("red")
        changed = {**self.store.photo(self.ids[0]), "content_version": hashlib.sha256(data).hexdigest()}
        with self.store.transaction():
            self.store.put_photo(changed)
            self.store.put_thumbnail(changed, data)
        self.assertEqual(feature_index.current_result(self.ids[0], store=self.store, profile=profile)["status"], "stale")
        self.execute(self.plan(profile, self.ids[:1]))
        new = feature_index.current_result(self.ids[0], store=self.store, profile=profile)["result"]
        self.assertNotEqual(old["input_fingerprint"], new["input_fingerprint"])
        self.assertNotEqual(old["result_id"], new["result_id"])
        history = self.store.feature_history(self.ids[0], fingerprint(profile))
        self.assertEqual({row["result_id"] for row in history}, {old["result_id"], new["result_id"]})

    def test_planned_reuse_cannot_expand_into_computation(self):
        profile = self.profile("objects")
        provider = Vision(profile)
        self.execute(self.plan(profile, self.ids[:1], provider=provider), provider=provider)
        cached = self.plan(profile, self.ids[:1], provider=provider)
        calls = provider.calls
        with patch.object(self.store, "find_feature_result", return_value=None):
            result = self.execute(cached, provider=provider)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["items"][0]["error"]["code"], "FEATURE_REPLAN_REQUIRED")
        self.assertEqual(provider.calls, calls)

    def test_runtime_version_is_part_of_color_and_hash_execution(self):
        from photography_lib.feature_algorithms import compute_color, compute_hash

        for component, compute in (("color", compute_color), ("perceptual_hash", compute_hash)):
            with self.subTest(component=component):
                profile = self.profile(component)
                with patch("PIL.__version__", "unmatched-version"):
                    self.error("FEATURE_RUNTIME_MISMATCH", compute, self.data, profile)

    def test_composition_uses_exact_objects_dependency_and_no_image_model(self):
        objects = self.profile("objects")
        composition = self.profile("composition", fingerprint(objects))
        missing = self.plan(composition)
        self.assertEqual(missing["counts"]["skip"], 3)
        provider = Vision(objects)
        self.assertEqual(self.execute(self.plan(objects, provider=provider), provider=provider)["status"], "completed")
        with patch("photography_lib.feature_inputs.stored_preview", side_effect=AssertionError("Composition needs no pixels")):
            result = self.execute(self.plan(composition))
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["model_calls"], 0)
        record = self.store.feature_result(result["items"][0]["result_id"])
        self.assertEqual(len(record["feature_dependencies"]), 1)
        self.assertAlmostEqual(record["payload"]["features"]["union_area"], .42)
        self.store.put_thumbnail(self.store.photo(self.ids[0]), self.jpeg("white"))
        self.assertEqual(feature_index.inspect_feature(self.ids[0], composition, store=self.store)[0]["status"],
                         "dependency_missing")

    def test_scene_prototypes_are_explicit_and_reuse_matching_existing_vectors(self):
        embedding_id = self.seed_embedding(self.ids[0])
        scene = self.profile("scene", embedding_id)
        missing = self.plan(scene, self.ids[:1])
        self.assertEqual(missing["counts"]["skip"], 1)
        encoder = TextEncoder()
        plan = feature_index.create_prototype_plan(store=self.store, config=self.config, profile=scene, encoder=encoder)
        self.assertEqual(encoder.calls, 0)
        with patch.object(self.store, "feature_run", wraps=self.store.feature_run) as reads:
            result = self.execute(plan, encoder=encoder)
        self.assertLessEqual(reads.call_count, 6, "Each text encoding must not reparse the complete run plan.")
        self.assertEqual(result["status"], "completed", result)
        self.assertGreater(encoder.calls, 0)
        self.assertEqual(result["model_calls"], encoder.calls)
        prototypes = self.store.scene_prototype_set(fingerprint(scene))
        self.assertIsNotNone(prototypes)
        with patch("photography_lib.feature_index._text_encoder", side_effect=AssertionError("Saved prototypes reused")):
            reuse = feature_index.create_prototype_plan(store=self.store, config=self.config, profile=scene)
            self.assertEqual(self.execute(reuse)["model_calls"], 0)
            result = self.execute(self.plan(scene, self.ids[:1]))
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["model_calls"], 0)
        record = self.store.feature_result(result["items"][0]["result_id"])
        self.assertEqual(len(record["payload"]["scores"]), len(prototypes["prototypes"]))
        self.assertEqual(len(record["embedding_dependencies"]), 1)

    def test_comparison_plans_are_frozen_and_store_pairs_without_loading_images(self):
        profile = self.profile("perceptual_hash")
        self.execute(self.plan(profile))
        for metric in ("hamming", "exact"):
            with self.subTest(metric=metric), patch("photography_lib.feature_inputs.stored_preview",
                                                  side_effect=AssertionError("Compare saved values only")):
                plan = feature_index.create_compare_plan(self.ids, store=self.store, config=self.config,
                                                          profile=profile, metric=metric, max_distance=0)
                self.assertEqual(plan["options"]["comparison_count"], 3)
                result = self.execute(plan)
                self.assertEqual(result["status"], "completed", result)
                pairs = self.store.similarity_pairs(plan["run_id"])
                self.assertEqual(pairs["total"], 3)
                self.assertEqual(result["model_calls"], 0)
                self.assertEqual(result["items"][0]["progress"]["comparisons"], 3)

    def test_compare_detects_stale_sources(self):
        profile = self.profile("perceptual_hash")
        self.execute(self.plan(profile))
        plan = feature_index.create_compare_plan(self.ids, store=self.store, config=self.config, profile=profile)
        self.store.put_thumbnail(self.store.photo(self.ids[0]), self.jpeg("white"))
        result = self.execute(plan)
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(self.store.similarity_pairs(plan["run_id"])["total"], 0)

    def test_compare_checkpoint_rollback_never_skips_uncommitted_pairs(self):
        profile = self.profile("perceptual_hash")
        with self.store.transaction():
            for i in range(4, 25):
                pid = f"p{i:02d}"
                self.store.put_photo({**self.store.photo(self.ids[0]), "photo_id": pid})
                self.ids.append(pid)
        transaction = self.store.transaction
        for checkpoint in (256, 276):
            for failure in (KeyboardInterrupt, sqlite3.OperationalError):
                with self.subTest(checkpoint=checkpoint, failure=failure.__name__):
                    plan = feature_index.create_compare_plan(self.ids, store=self.store, config=self.config,
                                                              profile=profile, metric="exact")
                    triggered = False

                    @contextmanager
                    def fail_before_commit():
                        nonlocal triggered
                        with transaction():
                            yield
                            current = self.store.feature_items(plan["run_id"])[0]
                            if not triggered and current["progress"].get("comparisons") == checkpoint:
                                triggered = True
                                raise failure()

                    with patch.object(self.store, "transaction", fail_before_commit):
                        interrupted = self.execute(plan)
                    self.assertTrue(triggered)
                    committed = 0 if checkpoint == 256 else 256
                    self.assertEqual(interrupted["items"][0]["progress"].get("comparisons", 0), committed)
                    self.assertEqual(self.store.similarity_pairs(plan["run_id"])["total"], committed)
                    result = feature_index.execute_plan(plan["run_id"], store=self.store, config=self.config, resume=True)
                    self.assertEqual(result["status"], "completed", result)
                    self.assertEqual(self.store.similarity_pairs(plan["run_id"])["total"], 276)

    def test_historical_ocr_result_has_independent_detail_pagination(self):
        profile = self.profile("ocr")
        provider = Vision(profile)
        payload = provider.compute(self.data)
        payload["blocks"] = [{**payload["blocks"][0], "text": f"line {i:03d}"} for i in range(101)]
        payload["text"] = "\n".join(block["text"] for block in payload["blocks"])
        payload["normalized_text"] = " ".join(payload["text"].split())
        with patch.object(provider, "compute", return_value=payload):
            run = self.execute(self.plan(profile, self.ids[:1], provider=provider), provider=provider)
        result_id = run["items"][0]["result_id"]
        changed = {**self.store.photo(self.ids[0]), "content_version": hashlib.sha256(b"changed").hexdigest()}
        self.store.put_photo(changed)
        code, detail = self.cli("index", "result", self.ids[0], "--component", "ocr", "--result-id", result_id,
                                "--details", "--after", "100", "--limit", "10")
        self.assertEqual(code, 0, detail)
        self.assertTrue(detail["historical"])
        self.assertEqual([block["text"] for block in detail["result"]["payload"]["blocks"]], ["line 100"])
        self.assertIsNone(detail["result"]["next_cursor"])
        self.assertEqual(self.cli("index", "result", self.ids[1], "--component", "ocr", "--result-id", result_id)[0], 2)

    def test_resume_requires_prior_approval_and_stopped_running_work(self):
        profile = self.profile("objects")
        provider = Vision(profile, lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        plan = self.plan(profile, self.ids[:1], provider=provider)
        self.error("FEATURE_CONFIRMATION_REQUIRED", feature_index.execute_plan, plan["run_id"],
                   store=self.store, config=self.config, resume=True, provider=provider)
        result = self.execute(plan, provider=provider)
        self.assertTrue(result["interrupted"])
        with self.store.transaction():
            self.store.update_feature_run(plan["run_id"], status="running")
        self.error("FEATURE_STOPPED_CONFIRMATION_REQUIRED", feature_index.execute_plan, plan["run_id"],
                   store=self.store, config=self.config, resume=True, provider=Vision(profile))
        result = feature_index.execute_plan(plan["run_id"], store=self.store, config=self.config,
                                            resume=True, confirm_stopped=True, provider=Vision(profile))
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["model_calls"], 2)

    def test_readonly_dry_run_and_current_lookup_preserve_database(self):
        profile = self.profile("color")
        self.execute(self.plan(profile))
        before = self.config.database_path.read_bytes()
        with SQLiteStorage.open(self.config.database_path) as reader:
            plan = feature_index.create_plan(self.ids, store=reader, config=self.config, profile=profile, persist=False)
            self.assertEqual(plan["status"], "dry_run")
            result = feature_index.status(store=reader, profile=profile, limit=1, status_filter="ready")
            self.assertEqual(len(result["items"]), 1)
            self.assertEqual(result["coverage"]["ready"], 3)
        self.assertEqual(self.config.database_path.read_bytes(), before)

    def test_preview_metadata_corruption_and_oversized_originals_fail_preflight(self):
        color = self.profile("color")
        self.execute(self.plan(color, self.ids[:1]))
        self.store.db.execute("UPDATE thumbnails SET mime_type='image/png' WHERE photo_id=?", (self.ids[0],))
        self.assertEqual(feature_index.inspect_feature(self.ids[0], color, store=self.store)[0]["status"], "invalid_input")
        ocr = self.profile("ocr")
        photo = self.store.photo(self.ids[1])
        self.store.put_photo({**photo, "size_bytes": feature_inputs.MAX_ORIGINAL_BYTES + 1})
        with patch("photography_lib.feature_inputs.resolve_original", side_effect=AssertionError("No original preflight reads")):
            plan = self.plan(ocr, self.ids[1:2], provider=Vision(ocr))
        self.assertEqual(plan["counts"]["skip"], 1)

    def test_cli_opt_in_profiles_execution_results_and_legacy_search(self):
        code, setup = self.cli("index", "setup", "--component", "color")
        self.assertEqual(code, 0, setup)
        self.assertIsNone(setup["default_profile_id"])
        code, missing = self.cli("index", "plan", "--component", "color", "--all")
        self.assertEqual(code, 2, missing)
        self.assertEqual(self.cli("index", "configure", "--component", "color",
                                  "--default-profile", setup["profile_id"])[0], 0)
        code, plan = self.cli("index", "plan", "--component", "color", "--all")
        self.assertEqual(code, 0, plan)
        code, result = self.cli("index", "execute", plan["run_id"], "--confirm", plan["digest"])
        self.assertEqual(code, 0, result)
        self.assertEqual(self.cli("index", "result", self.ids[0], "--component", "color", "--details")[0], 0)
        code, history = self.cli("index", "result-history", self.ids[0], "--component", "color", "--limit", "1")
        self.assertEqual(code, 0, history)
        self.assertEqual(len(history["items"]), 1)
        self.assertIsNone(history["next_cursor"])
        self.assertEqual(self.cli("index", "profiles")[1]["component"], "image_embedding")
        self.assertEqual(self.cli("management", "search", "p01", "--mode", "metadata")[0], 0)
        self.assertEqual(management.photos(store=self.store)["scope"], {"kind": "album"})

    def test_cli_invalid_component_options_and_fts_rebuild_are_explicit(self):
        self.assertEqual(self.cli("index", "setup", "--component", "composition")[0], 2)
        self.assertEqual(self.cli("index", "setup", "--dependency-profile-id", "anything")[0], 2)
        self.assertEqual(self.cli("index", "rebuild-fts", "--confirm")[0], 0)
        before = self.config.database_path.read_bytes()
        self.assertEqual(self.cli("index", "status", "--status", "invalid_result")[0], 2)
        self.assertEqual(self.config.database_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
