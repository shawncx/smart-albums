"""Synthetic image-embedding tests: no model loads, downloads, originals or live databases."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from PIL import Image
from photography_lib.fingerprints import fingerprint
from photography_lib.config import Config, PhotographyError
from photography_lib.index_lock import execution_lock
from photography_lib.image_embedding_storage import IMAGE_EMBEDDING_TABLES, profile_identity, timestamp
from photography_lib.image_embedding import create_plan, execute_plan, embedding_status, inspect_embedding, job, resolve_profile
from photography_lib import image_embedding
from photography_lib.sqlite_storage import SQLiteStorage


PROFILE = {"profile_schema": "image-embedding-profile-v1", "embedding_kind": "image_text_semantic",
           "stored_modality": "image", "input_scope": "stored_thumbnail", "granularity": "whole_image",
           "model": "synthetic-no-inference", "revision": "fixture-v1", "dimensions": 3,
           "dtype": "float32-le", "normalized": True}


def jpeg(color="navy"):
    output = io.BytesIO()
    Image.new("RGB", (96, 48), color).save(output, "JPEG")
    return output.getvalue()


class FakeEncoder:
    def __init__(self, profile=None, callback=None):
        self.identity = copy.deepcopy(profile or PROFILE)
        self.calls = 0
        self.profile_calls = 0
        self.callback = callback

    def profile(self):
        self.profile_calls += 1
        return copy.deepcopy(self.identity)

    def encode_image(self, data):
        self.calls += 1
        if self.callback:
            self.callback(data)
        return SimpleNamespace(vector=[1.0, 0.0, 0.0], elapsed_seconds=0.01, token_count=None)


class CacheOnlyEncoder:
    def profile(self):
        raise AssertionError("A cache hit must not consult or load an encoder.")

    def encode_image(self, data):
        raise AssertionError("A cache hit must not run inference.")


class ImageEmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.base = PROJECT / (".image-embedding-tests-" + uuid4().hex)
        self.base.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.base))
        self.config = Config(self.base / "album.sqlite", model_cache_root=self.base / "models", thumbnail_size=128)
        self.store = SQLiteStorage.create(self.config.database_path)
        self.addCleanup(lambda: self.store.close())
        self.ids = ["photo_1", "photo_2", "photo_3"]
        with self.store.transaction():
            for photo_id in self.ids:
                photo = {
                    "photo_id": photo_id, "ingest_state": "available", "original_status": "not_checked",
                    "content_version": "version-1", "thumbnail_profile": self.config.thumbnail_profile,
                    "original_absolute_path": str(self.base / "does-not-exist" / f"{photo_id}.jpg"),
                    "original_relative_path": str(Path("does-not-exist") / f"{photo_id}.jpg"),
                    "size_bytes": 123, "mtime_ns": 123456789, "metadata": {},
                    "created_at": timestamp(), "updated_at": timestamp(),
                }
                self.store.put_photo(photo)
                self.store.put_thumbnail(photo, jpeg())

    def plan(self, ids=None, profile=None, **kwargs):
        return create_plan(self.ids if ids is None else ids, store=self.store, config=self.config,
                           profile=profile or PROFILE, **kwargs)

    def execute(self, plan, encoder=None, **kwargs):
        return execute_plan(plan["run_id"], store=self.store, config=self.config,
                            encoder=encoder or FakeEncoder(), **kwargs)

    def generate(self, ids=None, profile=None, encoder=None):
        plan = self.plan(ids, profile)
        result = self.execute(plan, encoder or FakeEncoder(profile), confirm=plan["digest"])
        self.assertEqual(result["status"], "completed")
        return result

    def change(self, photo_id, *, version=None, color=None, state=None):
        photo = self.store.photo(photo_id)
        if version:
            photo["content_version"] = version
        if state:
            photo["ingest_state"] = state
        with self.store.transaction():
            self.store.put_photo(photo)
            if color:
                self.store.put_thumbnail(photo, jpeg(color))

    def result_count(self):
        return self.store.db.execute("SELECT COUNT(*) FROM image_embedding_results").fetchone()[0]

    def test_fresh_schema_eight_and_independent_profile_configuration(self):
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 8)
        tables = {row[0] for row in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(set(IMAGE_EMBEDDING_TABLES).issubset(tables))
        self.assertFalse(any(name.startswith(("image_index_", "technical_")) for name in tables))
        self.assertIsNone(self.store.default_embedding_profile())
        with self.assertRaises(PhotographyError) as raised:
            resolve_profile(self.store)
        self.assertEqual(raised.exception.code, "INDEX_CONFIGURATION_REQUIRED")
        profile_id = self.store.put_embedding_profile(PROFILE)
        self.assertEqual(profile_id, fingerprint(PROFILE))
        self.assertEqual(self.store.put_embedding_profile(PROFILE), profile_id)
        self.assertEqual(self.store.embedding_profiles()[0]["profile"], PROFILE)
        self.assertIsNone(self.store.default_embedding_profile())
        self.store.set_default_embedding_profile(profile_id)
        self.assertEqual(resolve_profile(self.store), PROFILE)
        with self.assertRaises(PhotographyError):
            self.store.set_default_embedding_profile("unknown")
        self.assertEqual(self.store.default_embedding_profile(), profile_id)
        self.assertIsNone(self.store.db.execute("SELECT name FROM sqlite_master WHERE name='analysis_settings'").fetchone())
        self.assertEqual(list(self.store.db.execute("PRAGMA foreign_key_check")), [])

    def test_profile_validation_and_sql_immutability(self):
        for bad in ({}, [], {"dimensions": float("nan")}, {"x": object()}):
            with self.subTest(profile=str(bad)), self.assertRaises(PhotographyError):
                self.store.put_embedding_profile(bad)
        profile_id = self.store.put_embedding_profile(PROFILE)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE image_embedding_profiles SET profile_json='{}' WHERE profile_id=?", (profile_id,))
        for bad in (0, True, 4097, "3"):
            with self.subTest(dimensions=bad), self.assertRaises(PhotographyError):
                self.plan(profile={**PROFILE, "dimensions": bad})

    def test_profile_requires_every_semantic_identity_field(self):
        for field in ("profile_schema", "embedding_kind", "stored_modality", "input_scope", "granularity"):
            missing = {key: value for key, value in PROFILE.items() if key != field}
            for bad in (missing, {**PROFILE, field: "not-the-supported-semantics"}):
                with self.subTest(field=field, profile=bad), self.assertRaises(PhotographyError) as raised:
                    profile_identity(bad)
                self.assertEqual(raised.exception.code, "INDEX_PROFILE_INVALID")

    def test_album_scope_is_explicit_and_envelopes_are_current(self):
        self.assertEqual([photo["photo_id"] for photo in self.store.photos()], self.ids)
        plan = self.plan(self.ids[:1])
        status = embedding_status(self.store.photos(), self.store, PROFILE)
        for result in (plan, status, job(self.store, plan["run_id"])):
            self.assertEqual(result["component"], "image_embedding")
            self.assertEqual(result["input_scope"], "stored_thumbnail")
            self.assertEqual(result["album"], {
                "id": self.store.album()["id"], "name": "album",
                "database_path": str(self.config.database_path),
            })
        self.assertEqual(plan["album_id"], self.store.album()["id"])

    def test_plan_requires_scope_and_dry_run_never_writes(self):
        for ids in ([], None, "photo_1", [""], [1], ["unknown"]):
            with self.subTest(ids=ids), self.assertRaises(PhotographyError):
                create_plan(ids, store=self.store, config=self.config, profile=PROFILE)
        plan = self.plan([self.ids[2], self.ids[0], self.ids[0]], persist=False)
        self.assertEqual(plan["status"], "dry_run")
        self.assertEqual([item["photo_id"] for item in plan["snapshots"]], [self.ids[0], self.ids[2]])
        self.assertEqual(plan["pending"], 2)
        self.assertEqual((plan["counts"]["pending"], plan["counts"]["cached"]), (2, 0))
        self.assertEqual(self.store.embedding_profiles(), [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_embedding_runs").fetchone()[0], 0)
        self.assertEqual(plan["model_calls"], 0)

    def test_confirmation_and_resume_cannot_grant_inference(self):
        plan = self.plan()
        for kwargs in ({}, {"confirm": "wrong"}, {"resume": True},
                       {"resume": True, "confirm": plan["digest"]},
                       {"resume": True, "confirm_stopped": True},
                       {"confirm_stopped": True, "confirm": plan["digest"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(PhotographyError):
                self.execute(plan, CacheOnlyEncoder(), **kwargs)
        self.assertFalse(job(self.store, plan["run_id"])["confirmed"])
        self.assertEqual(self.result_count(), 0)

    def test_inference_and_readiness_cannot_run_in_an_outer_transaction(self):
        plan = self.plan(self.ids[:1])
        with self.store.transaction():
            with self.assertRaises(PhotographyError) as raised:
                self.execute(plan, CacheOnlyEncoder(), confirm=plan["digest"])
            self.assertEqual(raised.exception.code, "INDEX_TRANSACTION_ACTIVE")
            with self.assertRaises(PhotographyError) as raised:
                self.plan(self.ids[:1], check_ready=CacheOnlyEncoder().profile)
            self.assertEqual(raised.exception.code, "INDEX_TRANSACTION_ACTIVE")
        self.assertFalse(job(self.store, plan["run_id"])["confirmed"])
        self.assertEqual(self.result_count(), 0)

    def test_lock_path_uses_actual_database_and_keeps_full_filename(self):
        expected = Path(str(self.config.database_path) + ".image-embedding.lock")
        self.assertEqual(self.store.embedding_lock_path, expected)
        other_path = self.base / "album.db"
        with SQLiteStorage.create(other_path) as other:
            self.assertEqual(other.embedding_lock_path, Path(str(other_path) + ".image-embedding.lock"))
            self.assertNotEqual(self.store.embedding_lock_path, other.embedding_lock_path)

    def test_read_only_status_plan_and_job_do_not_write_or_load(self):
        plan = self.plan(self.ids[:1])
        before = self.config.database_path.read_bytes()
        with SQLiteStorage.open(self.config.database_path) as readonly:
            status = embedding_status(readonly.photos(), readonly, PROFILE)
            saved = job(readonly, plan["run_id"])
            dry = create_plan(self.ids[:1], store=readonly, config=self.config, profile=PROFILE, persist=False)
            self.assertEqual(status["counts"]["missing"], 3)
            self.assertEqual(saved["digest"], plan["digest"])
            self.assertEqual(dry["pending"], 1)
        self.assertEqual(self.config.database_path.read_bytes(), before)
        self.assertFalse(self.store.embedding_lock_path.exists())

    def test_plan_readiness_runs_once_outside_transaction_before_persistence(self):
        checks = []

        def check_ready():
            self.assertFalse(self.store.db.in_transaction)
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_embedding_runs").fetchone()[0], 0)
            self.assertEqual(self.store.embedding_profiles(), [])
            checks.append("ready")
            self.change(self.ids[0], version="version-2", color="red")

        plan = self.plan(self.ids[:1], check_ready=check_ready)
        self.assertEqual(checks, ["ready"])
        self.assertEqual(plan["snapshots"][0]["content_version"], "version-1")
        result = self.execute(plan, CacheOnlyEncoder(), confirm=plan["digest"])
        self.assertEqual((result["counts"]["stale"], result["model_calls"]), (1, 0))

    def test_failed_readiness_never_persists_plan_or_profile(self):
        def check_ready():
            raise PhotographyError("MODEL_NOT_INSTALLED", "Synthetic installation failure.")

        with self.assertRaises(PhotographyError):
            self.plan(check_ready=check_ready)
        self.assertEqual(self.store.embedding_profiles(), [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_embedding_runs").fetchone()[0], 0)

    def test_cache_only_plan_skips_readiness_and_dry_run_checks_without_writing(self):
        self.generate(self.ids[:1])
        with patch.object(CacheOnlyEncoder, "profile", side_effect=AssertionError("Readiness called")) as check:
            cached = self.plan(self.ids[:1], check_ready=check)
        self.assertEqual(cached["counts"]["pending"], 0)
        before = self.store.db.execute("SELECT COUNT(*) FROM image_embedding_runs").fetchone()[0]
        checks = []
        dry = self.plan(self.ids[1:], persist=False, check_ready=lambda: checks.append(True))
        self.assertEqual((dry["counts"]["pending"], checks), (2, [True]))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_embedding_runs").fetchone()[0], before)

    def test_encode_saved_preview_without_originals_or_analysis_then_reuse_without_encoder(self):
        encoder = FakeEncoder(callback=lambda data: self.assertEqual(data, jpeg()))
        first = self.generate(encoder=encoder)
        self.assertEqual((encoder.calls, first["model_calls"], self.result_count()), (3, 3, 3))
        self.assertEqual(first["counts"]["indexed"], 3)
        self.assertIsNone(self.store.default_embedding_profile())
        self.assertIsNone(self.store.db.execute("SELECT name FROM sqlite_master WHERE name='analyses'").fetchone())
        for result in self.store.db.execute("SELECT * FROM image_embedding_results"):
            self.assertEqual(result["vector"], struct.pack("<3f", 1, 0, 0))
            self.assertEqual(result["vector_hash"], hashlib.sha256(result["vector"]).hexdigest())
        cached = self.plan()
        self.assertEqual((cached["cached"], cached["pending"]), (3, 0))
        self.assertEqual((cached["counts"]["cached"], cached["counts"]["pending"]), (3, 0))
        again = self.execute(cached, CacheOnlyEncoder())
        self.assertEqual((again["status"], again["model_calls"], again["counts"]["cached"]), ("completed", 0, 3))

    def test_cache_only_interrupted_run_resumes_without_confirmation_or_encoding(self):
        self.generate()
        plan = self.plan()
        original = image_embedding._current_input
        checks = 0

        def interrupt(item, store):
            nonlocal checks
            checks += 1
            if checks == 2:
                raise KeyboardInterrupt()
            return original(item, store)

        with patch.object(image_embedding, "_current_input", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.execute(plan, CacheOnlyEncoder())
        interrupted = job(self.store, plan["run_id"])
        self.assertFalse(interrupted["confirmed"])
        self.assertEqual(interrupted["counts"]["cached"], 1)
        with self.assertRaises(PhotographyError) as raised:
            self.execute(plan, CacheOnlyEncoder(), resume=True)
        self.assertEqual(raised.exception.code, "INDEX_STOPPED_CONFIRMATION_REQUIRED")
        resumed = self.execute(plan, CacheOnlyEncoder(), resume=True, confirm_stopped=True)
        self.assertEqual((resumed["status"], resumed["model_calls"], resumed["counts"]["cached"]),
                         ("completed", 0, 3))

    def test_cache_only_resume_does_not_expand_into_encoding(self):
        self.generate()
        plan = self.plan()
        with patch.object(image_embedding, "_current_input", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.execute(plan, CacheOnlyEncoder())
        self.store.db.execute("UPDATE image_embedding_results SET vector=? WHERE photo_id=?",
                              (b"damaged", self.ids[0]))
        resumed = self.execute(plan, CacheOnlyEncoder(), resume=True, confirm_stopped=True)
        self.assertEqual(resumed["status"], "partial")
        self.assertEqual(resumed["model_calls"], 0)
        self.assertEqual(resumed["items"][0]["error"]["code"], "INDEX_REPLAN_REQUIRED")

    def test_status_never_reads_original_or_thumbnail_blob(self):
        self.generate(self.ids[:1])
        photos = self.store.photos()
        reads = []

        def authorizer(action, table, column, *args):
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
                if table == "thumbnails" and column == "data":
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store.db.set_authorizer(authorizer)
        try:
            with patch.object(Path, "stat", side_effect=AssertionError("Original stat forbidden")):
                result = embedding_status(photos, self.store, PROFILE)
        finally:
            self.store.db.set_authorizer(None)
        self.assertEqual(result["counts"], {"total": 3, "ready": 1, "missing": 2,
                                          "stale": 0, "invalid_input": 0, "invalid_vector": 0})
        self.assertEqual(result["preview_integrity"], "unchecked")
        self.assertTrue(all(entry["preview_integrity"] == "unchecked" for entry in result["items"]))
        self.assertNotIn(("thumbnails", "data"), reads)

    def test_missing_original_allowed_ingestion_error_rejected_even_with_saved_vector(self):
        photo = self.store.photo(self.ids[0])
        photo["original_status"] = "missing"
        self.store.put_photo(photo)
        self.generate(self.ids[:1])
        self.change(self.ids[0], state="error")
        entry, _, _ = inspect_embedding(self.store.photo(self.ids[0]), self.store, PROFILE)
        self.assertEqual(entry["status"], "invalid_input")
        plan = self.plan(self.ids[:1])
        self.assertEqual(plan["snapshots"][0]["action"], "skip")
        result = self.execute(plan, CacheOnlyEncoder())
        self.assertEqual(result["counts"]["invalid_input"], 1)
        self.assertEqual(self.result_count(), 1)

    def test_original_status_and_relocation_do_not_invalidate_saved_vectors(self):
        self.generate(self.ids[:1])
        for state in ("not_checked", "available", "missing", "unavailable"):
            with self.subTest(original_status=state):
                photo = self.store.photo(self.ids[0])
                photo.update(original_status=state, original_absolute_path=str(self.base / "relocated.jpg"),
                             original_relative_path="relocated.jpg")
                self.store.put_photo(photo)
                cached = self.plan(self.ids[:1])
                self.assertEqual(cached["pending"], 0)
                self.assertEqual(self.execute(cached, CacheOnlyEncoder())["counts"]["cached"], 1)
        self.assertEqual(self.result_count(), 1)

    def test_metadata_and_full_preview_validation_are_distinct(self):
        self.generate(self.ids[:1])
        self.store.db.execute("UPDATE thumbnails SET data=? WHERE photo_id=?", (b"corrupt", self.ids[0]))
        photo = self.store.photo(self.ids[0])
        self.assertEqual(inspect_embedding(photo, self.store, PROFILE)[0]["status"], "ready")
        self.assertEqual(inspect_embedding(photo, self.store, PROFILE, check_preview=True)[0]["status"], "invalid_input")
        plan = self.plan(self.ids[:1])
        self.assertEqual(plan["counts"]["invalid_input"], 1)
        self.assertEqual(self.execute(plan, CacheOnlyEncoder())["model_calls"], 0)

    def test_invalid_preview_metadata_rejected_without_blob(self):
        for field, value in (("profile", "different"), ("content_version", "different"),
                             ("image_hash", "bad"), ("size_bytes", 0), ("width", 9999), ("mime_type", "image/png")):
            with self.subTest(field=field):
                original = self.store.thumbnail(self.ids[0], include_data=False)[field]
                self.store.db.execute(f"UPDATE thumbnails SET {field}=? WHERE photo_id=?", (value, self.ids[0]))
                self.assertEqual(inspect_embedding(self.store.photo(self.ids[0]), self.store, PROFILE)[0]["status"], "invalid_input")
                self.store.db.execute(f"UPDATE thumbnails SET {field}=? WHERE photo_id=?", (original, self.ids[0]))

    def test_versions_and_other_profiles_remain_separate_history(self):
        self.generate(self.ids[:1])
        second_profile = {**PROFILE, "revision": "fixture-v2"}
        self.assertEqual(inspect_embedding(self.store.photo(self.ids[0]), self.store, second_profile)[0]["status"], "missing")
        self.generate(self.ids[:1], second_profile)
        self.change(self.ids[0], version="version-2", color="red")
        self.assertEqual(inspect_embedding(self.store.photo(self.ids[0]), self.store, PROFILE)[0]["status"], "stale")
        self.generate(self.ids[:1])
        self.assertEqual(self.result_count(), 3)
        self.assertEqual(len(self.store.embedding_profiles()), 2)
        self.assertEqual(inspect_embedding(self.store.photo(self.ids[0]), self.store, second_profile)[0]["status"], "stale")

    def test_changed_hash_and_thumbnail_profile_are_part_of_result_key(self):
        self.generate(self.ids[:1])
        self.change(self.ids[0], color="red")
        self.generate(self.ids[:1])
        photo = self.store.photo(self.ids[0])
        photo["thumbnail_profile"] = "preview-fixture-v2"
        with self.store.transaction():
            self.store.put_photo(photo)
            self.store.put_thumbnail(photo, jpeg("red"))
        self.generate(self.ids[:1])
        self.assertEqual(self.result_count(), 3)
        self.assertEqual(self.store.db.execute("SELECT COUNT(DISTINCT content_version) FROM image_embedding_results").fetchone()[0], 1)

    def test_corrupt_vectors_repaired_in_place_without_deleting_history(self):
        self.generate(self.ids[:1])
        other = {**PROFILE, "revision": "keep-history"}
        self.generate(self.ids[:1], other)
        record = self.store.db.execute("SELECT * FROM image_embedding_results WHERE profile_id=?", (fingerprint(PROFILE),)).fetchone()
        for blob, vector_hash, dimensions in (
            (b"short", hashlib.sha256(b"short").hexdigest(), 3),
            (struct.pack("<3f", float("nan"), 0, 0), None, 3),
            (struct.pack("<3f", 0, 0, 0), None, 3),
            (struct.pack("<3f", 2, 0, 0), None, 3),
            (record["vector"], "wrong", 3),
            (record["vector"], record["vector_hash"], 2),
        ):
            with self.subTest(blob=blob, dimensions=dimensions):
                vector_hash = vector_hash or hashlib.sha256(blob).hexdigest()
                self.store.db.execute("""UPDATE image_embedding_results SET vector=?,vector_hash=?,dimensions=?
                    WHERE result_id=?""", (blob, vector_hash, dimensions, record["result_id"]))
                entry, _, vector = inspect_embedding(self.store.photo(self.ids[0]), self.store, PROFILE)
                self.assertEqual(entry["status"], "invalid_vector")
                self.assertIsNone(vector)
                repaired = self.generate(self.ids[:1])
                self.assertEqual(repaired["items"][0]["result_id"], record["result_id"])
                self.assertEqual(self.result_count(), 2)

    def test_invalid_encoder_vector_is_not_saved_and_stops_blind_retries(self):
        encoder = FakeEncoder()
        encoder.encode_image = lambda data: SimpleNamespace(vector=[float("nan"), 0, 0], elapsed_seconds=None)
        plan = self.plan()
        result = self.execute(plan, encoder, confirm=plan["digest"])
        self.assertEqual((result["counts"]["failed"], result["counts"]["pending"], self.result_count()), (1, 2, 0))
        self.assertEqual(result["model_calls"], 1)

    def test_input_change_before_execution_does_not_infer_or_expand_scope(self):
        plan = self.plan(self.ids[:1])
        self.change(self.ids[0], version="version-2", color="red")
        result = self.execute(plan, CacheOnlyEncoder(), confirm=plan["digest"])
        self.assertEqual((result["counts"]["stale"], result["counts"]["total"], result["model_calls"]), (1, 1, 0))
        self.assertEqual(self.result_count(), 0)

    def test_input_changes_during_inference_cannot_commit_success(self):
        for change in ({"version": "version-2", "color": "red"}, {"color": "green"}, {"state": "error"}):
            with self.subTest(change=change):
                self.change(self.ids[0], version="version-1", color="navy", state="available")
                plan = self.plan(self.ids[:1])

                def callback(data):
                    self.assertFalse(self.store.db.in_transaction)
                    with SQLiteStorage.open(self.config.database_path, writable=True) as other:
                        photo = other.photo(self.ids[0])
                        photo["content_version"] = change.get("version", photo["content_version"])
                        photo["ingest_state"] = change.get("state", photo["ingest_state"])
                        with other.transaction():
                            other.put_photo(photo)
                            if "color" in change:
                                other.put_thumbnail(photo, jpeg(change["color"]))

                result = self.execute(plan, FakeEncoder(callback=callback), confirm=plan["digest"])
                self.assertNotEqual(result["status"], "completed")
                self.assertEqual(result["counts"]["indexed"], 0)
                self.assertEqual(self.result_count(), 0)
                self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_embedding_claims").fetchone()[0], 0)

    def test_preview_corrupt_after_plan_fails_before_encoder(self):
        plan = self.plan(self.ids[:1])
        self.store.db.execute("UPDATE thumbnails SET data=? WHERE photo_id=?", (b"bad", self.ids[0]))
        result = self.execute(plan, CacheOnlyEncoder(), confirm=plan["digest"])
        self.assertEqual((result["counts"]["invalid_input"], result["model_calls"]), (1, 0))

    def test_invalidated_planned_cache_hit_requires_new_repair_plan(self):
        self.generate(self.ids[:1])
        plan = self.plan(self.ids[:1])
        self.store.db.execute("UPDATE image_embedding_results SET vector_hash='bad'")
        result = self.execute(plan, CacheOnlyEncoder(), confirm=plan["digest"])
        self.assertEqual(result["counts"]["failed"], 1)
        self.assertEqual(result["items"][0]["error"]["code"], "INDEX_REPLAN_REQUIRED")
        self.assertEqual(result["model_calls"], 0)

    def test_plan_and_snapshot_immutable_and_return_values_detached(self):
        plan = self.plan(self.ids[:1])
        identity = copy.deepcopy(plan["profile"])
        plan["profile"]["revision"] = "caller-mutated"
        plan["snapshots"][0]["content_version"] = "caller-mutated"
        self.assertEqual(job(self.store, plan["run_id"])["profile"], identity)
        for sql in (
            "UPDATE image_embedding_runs SET digest='changed'",
            "UPDATE image_embedding_runs SET plan_json='{}'",
            "UPDATE image_embedding_items SET action='skip'",
            "UPDATE image_embedding_items SET content_version='changed'",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.db.execute(sql)
        original = job(self.store, plan["run_id"])
        self.assertEqual(self.execute(original, confirm=original["digest"])["status"], "completed")

    def test_digest_tampering_rejected_before_confirmation_or_encoder(self):
        plan = self.plan(self.ids[:1])
        self.store.db.execute("DROP TRIGGER image_embedding_plan_immutable")
        changed = copy.deepcopy(plan)
        changed["snapshots"][0]["action"] = "skip"
        self.store.db.execute("UPDATE image_embedding_runs SET plan_json=?", (json.dumps(changed),))
        with self.assertRaises(PhotographyError) as raised:
            self.execute(plan, CacheOnlyEncoder(), confirm=plan["digest"])
        self.assertEqual(raised.exception.code, "INDEX_PLAN_INVALID")
        self.assertEqual(self.result_count(), 0)

    def test_component_album_and_pending_tampering_fail_integrity_checks(self):
        plan = self.plan(self.ids[:1])
        self.store.db.execute("DROP TRIGGER image_embedding_plan_immutable")
        original = self.store.db.execute("SELECT plan_json FROM image_embedding_runs").fetchone()[0]
        for field, value in (("component", "technical"), ("album_id", "another-album"), ("pending", 0)):
            changed = json.loads(original)
            changed[field] = value
            with self.subTest(field=field):
                self.store.db.execute("UPDATE image_embedding_runs SET plan_json=?", (json.dumps(changed),))
                with self.assertRaises(PhotographyError) as raised:
                    self.execute(plan, CacheOnlyEncoder(), confirm=plan["digest"])
                self.assertEqual(raised.exception.code, "INDEX_PLAN_INVALID")
                self.assertEqual(self.result_count(), 0)

    def test_plan_copied_to_another_album_is_not_authorized(self):
        plan = self.plan(self.ids[:1])
        with SQLiteStorage.create(self.base / "other.sqlite") as other:
            with other.transaction():
                other.put_photo(self.store.photo(self.ids[0]))
                other.put_thumbnail(other.photo(self.ids[0]), jpeg())
                other.put_embedding_profile(PROFILE)
                other._create_embedding_run(plan)
            with self.assertRaises(PhotographyError) as raised:
                execute_plan(plan["run_id"], store=other, config=self.config, encoder=CacheOnlyEncoder(),
                             confirm=plan["digest"])
            self.assertEqual(raised.exception.code, "INDEX_PLAN_INVALID")
            self.assertEqual(other.db.execute("SELECT COUNT(*) FROM image_embedding_results").fetchone()[0], 0)

    def test_stopped_album_file_move_preserves_scope_and_uses_current_envelope(self):
        plan = self.plan(self.ids[:1])
        original_album = self.store.album()["id"]
        self.store.close()
        destination = self.base / "renamed.sqlite3"
        self.config.database_path.rename(destination)
        self.store = SQLiteStorage.open(destination, writable=True)
        moved = job(self.store, plan["run_id"])
        self.assertEqual(moved["digest"], plan["digest"])
        self.assertEqual(moved["album"], {
            "id": original_album, "name": "renamed", "database_path": str(destination),
        })
        self.assertEqual(self.store.embedding_lock_path, Path(str(destination) + ".image-embedding.lock"))
        self.assertEqual(self.execute(plan, confirm=plan["digest"])["status"], "completed")

    def test_copied_running_album_needs_stopped_confirmation_before_recovery(self):
        plan = self.plan(self.ids[:1])
        encoder = FakeEncoder(callback=lambda data: (_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            self.execute(plan, encoder, confirm=plan["digest"])
        self.store.close()
        destination = self.base / "transferred.db"
        shutil.copyfile(self.config.database_path, destination)
        with SQLiteStorage.open(destination, writable=True) as transferred:
            with self.assertRaises(PhotographyError) as raised:
                execute_plan(plan["run_id"], store=transferred, config=self.config,
                             encoder=CacheOnlyEncoder(), resume=True)
            self.assertEqual(raised.exception.code, "INDEX_STOPPED_CONFIRMATION_REQUIRED")
            with execution_lock(transferred.embedding_lock_path):
                with self.assertRaises(PhotographyError) as raised:
                    execute_plan(plan["run_id"], store=transferred, config=self.config,
                                 encoder=CacheOnlyEncoder(), resume=True, confirm_stopped=True)
                self.assertEqual(raised.exception.code, "INDEX_IN_PROGRESS")
            resumed = execute_plan(plan["run_id"], store=transferred, config=self.config,
                                   encoder=FakeEncoder(), resume=True, confirm_stopped=True)
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(resumed["album"]["database_path"], str(destination))
            self.assertEqual(resumed["digest"], plan["digest"])
            self.assertEqual(resumed["items"][0]["attempts"][0]["status"], "interrupted")
        self.store = SQLiteStorage.open(self.config.database_path, writable=True)

    def test_other_runs_cannot_implicitly_recover_unconfirmed_stopped_work(self):
        abandoned, another = self.plan(self.ids[:1]), self.plan(self.ids[:2])
        encoder = FakeEncoder(callback=lambda data: (_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            self.execute(abandoned, encoder, confirm=abandoned["digest"])
        partial = self.execute(another, confirm=another["digest"])
        self.assertEqual(partial["items"][0]["error"]["code"], "INDEX_IN_PROGRESS")
        self.assertEqual(partial["counts"]["indexed"], 1)
        self.assertEqual(job(self.store, abandoned["run_id"])["status"], "running")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_embedding_claims").fetchone()[0], 1)
        with self.assertRaises(PhotographyError) as raised:
            self.execute(abandoned, CacheOnlyEncoder(), resume=True)
        self.assertEqual(raised.exception.code, "INDEX_STOPPED_CONFIRMATION_REQUIRED")

    def test_added_item_cannot_expand_confirmed_scope(self):
        plan = self.plan(self.ids[:1])
        original = self.store.db.execute("SELECT * FROM image_embedding_items").fetchone()
        values = list(original)
        columns = list(original.keys())
        values[columns.index("photo_id")] = self.ids[1]
        snapshot = json.loads(original["snapshot_json"])
        snapshot["photo_id"] = self.ids[1]
        values[columns.index("snapshot_json")] = json.dumps(snapshot)
        self.store.db.execute(f"INSERT INTO image_embedding_items VALUES ({','.join('?' for _ in values)})", values)
        with self.assertRaises(PhotographyError) as raised:
            self.execute(plan, CacheOnlyEncoder(), confirm=plan["digest"])
        self.assertEqual(raised.exception.code, "INDEX_PLAN_INVALID")

    def test_profile_mismatch_fails_without_model_call(self):
        plan = self.plan()
        encoder = FakeEncoder({**PROFILE, "revision": "other"})
        result = self.execute(plan, encoder, confirm=plan["digest"])
        self.assertEqual((encoder.calls, result["model_calls"], result["counts"]["pending"]), (0, 0, 2))
        self.assertEqual(result["items"][0]["error"]["code"], "INDEX_PROFILE_MISMATCH")

    def test_result_and_task_success_are_one_atomic_transaction(self):
        plan = self.plan(self.ids[:1])
        write_item = self.store._update_embedding_item
        failed = False

        def fail_once(run_id, item):
            nonlocal failed
            if item["status"] == "indexed" and not failed:
                failed = True
                raise sqlite3.OperationalError("Simulated task-item save failure")
            return write_item(run_id, item)

        with patch.object(self.store, "_update_embedding_item", side_effect=fail_once):
            result = self.execute(plan, confirm=plan["digest"])
        self.assertEqual((result["counts"]["failed"], self.result_count()), (1, 0))
        self.assertEqual(result["items"][0]["attempts"][0]["status"], "failed")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_embedding_claims").fetchone()[0], 0)
        resumed = self.execute(plan, resume=True)
        self.assertEqual((resumed["status"], resumed["model_calls"]), ("completed", 2))

    def test_crash_resume_preserves_success_and_retries_only_unpersisted_work(self):
        plan = self.plan()
        encoder = FakeEncoder()

        def crash(data):
            if encoder.calls == 2:
                raise KeyboardInterrupt()

        encoder.callback = crash
        with self.assertRaises(KeyboardInterrupt):
            self.execute(plan, encoder, confirm=plan["digest"])
        interrupted = job(self.store, plan["run_id"])
        self.assertEqual((interrupted["status"], interrupted["counts"]["indexed"]), ("running", 1))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_embedding_claims").fetchone()[0], 1)
        with self.assertRaises(PhotographyError):
            self.execute(plan)
        with self.assertRaises(PhotographyError) as raised:
            self.execute(plan, CacheOnlyEncoder(), resume=True)
        self.assertEqual(raised.exception.code, "INDEX_STOPPED_CONFIRMATION_REQUIRED")
        self.assertEqual(job(self.store, plan["run_id"])["status"], "running")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_embedding_claims").fetchone()[0], 1)
        resumed_encoder = FakeEncoder()
        resumed = self.execute(plan, resumed_encoder, resume=True, confirm_stopped=True)
        self.assertEqual((resumed_encoder.calls, resumed["counts"]["indexed"], self.result_count()), (2, 3, 3))
        self.assertEqual(len(resumed["items"][0]["attempts"]), 1)
        self.assertEqual(len(resumed["items"][1]["attempts"]), 2)
        self.assertEqual(resumed["items"][1]["attempts"][0]["status"], "interrupted")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_embedding_claims").fetchone()[0], 0)

    def test_completed_execute_and_resume_skip_valid_success(self):
        result = self.generate()
        self.assertEqual(self.execute(result, CacheOnlyEncoder())["model_calls_this_execution"], 0)
        resumed = self.execute(result, CacheOnlyEncoder(), resume=True)
        self.assertEqual((resumed["model_calls_this_execution"], resumed["counts"]["indexed"]), (0, 3))
        self.assertEqual(len(resumed["items"][0]["attempts"]), 1)

    def test_resume_repairs_corrupt_success_but_skips_other_valid_successes(self):
        result = self.generate()
        damaged_id = result["items"][0]["result_id"]
        self.store.db.execute("UPDATE image_embedding_results SET vector_hash='bad' WHERE result_id=?", (damaged_id,))
        encoder = FakeEncoder()
        resumed = self.execute(result, encoder, resume=True)
        self.assertEqual((encoder.calls, resumed["model_calls_this_execution"], self.result_count()), (1, 1, 3))
        self.assertEqual(resumed["items"][0]["result_id"], damaged_id)
        self.assertEqual([len(item["attempts"]) for item in resumed["items"]], [2, 1, 1])

    def test_missing_model_failure_pauses_work_and_only_explicit_resume_retries(self):
        plan = self.plan()

        def unavailable(data):
            raise PhotographyError("MODEL_NOT_INSTALLED", "Synthetic model not installed.")

        encoder = FakeEncoder(callback=unavailable)
        failed = self.execute(plan, encoder, confirm=plan["digest"])
        self.assertEqual((encoder.calls, failed["counts"]["failed"], failed["counts"]["pending"]), (1, 1, 2))
        with self.assertRaises(PhotographyError):
            self.execute(plan)
        repaired = self.execute(plan, resume=True)
        self.assertEqual((repaired["status"], repaired["model_calls_this_execution"]), ("completed", 3))

    def test_builtin_float32_little_endian_profile_can_plan_without_model(self):
        from photography_lib.image_embedding_profiles import default_profile
        plan = self.plan(self.ids[:1], default_profile(), persist=False)
        self.assertEqual((plan["profile"]["dimensions"], plan["pending"], plan["model_calls"]), (768, 1, 0))

    def test_cross_run_cache_rechecked_before_encoder_profile(self):
        first, second = self.plan(self.ids[:1]), self.plan(self.ids[:1])
        self.execute(first, confirm=first["digest"])
        result = self.execute(second, CacheOnlyEncoder(), confirm=second["digest"])
        self.assertEqual((result["model_calls"], result["counts"]["cached"], self.result_count()), (0, 1, 1))

    def test_foreign_keys_forbid_result_of_another_photo_profile_or_input(self):
        result = self.generate(self.ids[:2])
        other = {**PROFILE, "revision": "other"}
        other_result = self.generate(self.ids[:1], other)
        for result_id in (result["items"][1]["result_id"], other_result["items"][0]["result_id"]):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.db.execute("UPDATE image_embedding_items SET result_id=? WHERE run_id=? AND photo_id=?",
                    (result_id, result["run_id"], self.ids[0]))
        self.change(self.ids[0], version="version-2", color="red")
        newer = self.generate(self.ids[:1])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE image_embedding_items SET result_id=? WHERE run_id=? AND photo_id=?",
                (newer["items"][0]["result_id"], result["run_id"], self.ids[0]))
        self.assertEqual(list(self.store.db.execute("PRAGMA foreign_key_check")), [])

    def test_cross_process_lock_refuses_active_executor_and_releases_after_crash(self):
        plan = self.plan(self.ids[:1])
        code = (
            "import sys,time\n"
            "from pathlib import Path\n"
            "from photography_lib.index_lock import execution_lock\n"
            "with execution_lock(Path(sys.argv[1])):\n"
            " print('locked', flush=True)\n"
            " time.sleep(120)\n"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        process = subprocess.Popen([sys.executable, "-c", code, str(self.store.embedding_lock_path)],
                                   cwd=PROJECT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaises(PhotographyError) as raised:
                self.execute(plan, CacheOnlyEncoder(), confirm=plan["digest"])
            self.assertEqual(raised.exception.code, "INDEX_IN_PROGRESS")
            self.assertEqual(job(self.store, plan["run_id"])["status"], "proposed")
            self.assertFalse(job(self.store, plan["run_id"])["confirmed"])
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        with execution_lock(self.store.embedding_lock_path):
            pass
        self.assertEqual(self.execute(plan, confirm=plan["digest"])["status"], "completed")

if __name__ == "__main__":
    unittest.main()
