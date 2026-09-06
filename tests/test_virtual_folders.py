"""Static folder management with isolated synthetic, project-local albums."""
from __future__ import annotations

import argparse
import builtins
from copy import deepcopy
import hashlib
import io
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

from PIL import Image
from photography_lib import management, virtual_folders as folders
from photography_lib.config import PhotographyError
from photography_lib.image_vectors import pack_vector
from photography_lib.sqlite_storage import SQLiteStorage
from photography_lib.text import fold_text


class FakeEncoder:
    def __init__(self, profile):
        self.identity = deepcopy(profile)
        self.queries = []

    def profile(self):
        return deepcopy(self.identity)

    def encode_text(self, query):
        self.queries.append(query)
        return argparse.Namespace(vector=(1.0, 0.0), elapsed_seconds=0.001, token_count=2)

    def encode_image(self, unused):
        raise AssertionError("Folder management must never encode an image.")


class VirtualFolderTests(unittest.TestCase):
    def setUp(self):
        self.base = PROJECT / (".virtual-folder-tests-" + uuid4().hex)
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base)
        self.path = self.base / "album.sqlite"
        self.store = SQLiteStorage.create(self.path)
        self.addCleanup(self.store.close)
        self.ids = ["photo_a", "photo_b", "photo_c", "photo_d"]
        for photo_id in self.ids:
            self.store.put_photo({
                "photo_id": photo_id,
                "original_absolute_path": str(self.base / "offline" / (photo_id + ".jpg")),
                "original_relative_path": "offline/" + photo_id + ".jpg",
                "content_version": hashlib.sha256(photo_id.encode()).hexdigest(),
                "thumbnail_profile": "preview-v1-srgb-128-q85",
                "size_bytes": 123, "mtime_ns": 1, "metadata": {"exif": {}},
                "ingest_state": "available", "original_status": "not_checked",
                "created_at": "2026-09-06T00:00:00+00:00", "updated_at": "2026-09-06T00:00:00+00:00",
            })
        original_import = builtins.__import__

        def guard(name, *args, **kwargs):
            if name.split(".")[0] in ("torch", "transformers", "numpy", "huggingface_hub", "tokenizers"):
                raise AssertionError("Folder management must not load an inference runtime.")
            return original_import(name, *args, **kwargs)

        runtime_guard = patch("builtins.__import__", side_effect=guard)
        runtime_guard.start()
        self.addCleanup(runtime_guard.stop)

    def create(self, name="冬天", **kwargs):
        return folders.create_folder(name, store=self.store, **kwargs)["folder"]

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(PhotographyError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def membership_rows(self, store=None):
        return [tuple(row) for row in (store or self.store).db.execute(
            "SELECT folder_id,photo_id,added_at FROM virtual_folder_photos ORDER BY folder_id,photo_id")]

    def folder_state(self, store=None):
        store = store or self.store
        return store.folders(), self.membership_rows(store)

    def physical_rows(self):
        return {table: [tuple(row) for row in self.store.db.execute(f"SELECT * FROM {table}")]
                for table in ("photos", "thumbnails", "image_embedding_profiles", "image_embedding_results",
                              "image_embedding_runs", "image_embedding_items", "image_embedding_claims",
                              "image_embedding_settings")}

    def configure(self):
        self.profile = {
            "profile_schema": "image-embedding-profile-v1", "embedding_kind": "image_text_semantic",
            "stored_modality": "image", "input_scope": "stored_thumbnail", "granularity": "whole_image",
            "model": "folder-offline-test", "dimensions": 2, "dtype": "float32-le", "normalized": True,
            "image": {"feature_api": "offline-image"}, "text": {"feature_api": "offline-text"},
        }
        self.profile_id = self.store.put_embedding_profile(self.profile)
        self.store.set_default_embedding_profile(self.profile_id)
        output = io.BytesIO()
        Image.new("RGB", (16, 16), "navy").save(output, format="JPEG")
        for photo_id in self.ids:
            self.store.put_thumbnail(self.store.photo(photo_id), output.getvalue())
        for photo_id, vector in zip(self.ids[:2], ((1.0, 0.0), (0.8, 0.6))):
            photo = self.store.photo(photo_id)
            snapshot = {**photo, "input_image_hash": self.store.thumbnail(photo_id, include_data=False)["image_hash"]}
            blob = pack_vector(vector, 2)
            self.store._put_embedding_result(snapshot, self.profile_id, blob, hashlib.sha256(blob).hexdigest(), 2)
        self.encoder = FakeEncoder(self.profile)

    def search(self, **kwargs):
        return management.semantic_search("合成照片", store=self.store, encoder=self.encoder, **kwargs)

    def test_create_custom_empty_folder_without_index_or_source_io(self):
        with patch.object(self.store, "thumbnail", side_effect=AssertionError("preview access")), \
             patch.object(self.store, "embedding_profile", side_effect=AssertionError("embedding access")), \
             patch.object(Path, "open", side_effect=AssertionError("original access")), \
             patch.object(Path, "stat", side_effect=AssertionError("original stat")):
            result = folders.create_folder("  冬天/精选  ", store=self.store, description="静态分类")
            folder = result["folder"]
            self.assertEqual(folder["name"], "冬天/精选")
            self.assertEqual(folder["description"], "静态分类")
            self.assertEqual(folder["photo_count"], 0)
            self.assertEqual(folder["created_at"], folder["updated_at"])
            self.assertEqual(result["counts"], {"created": 1})
            self.assertEqual(result["album"], self.store.album())
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(result["image_model_calls"], 0)
            self.assertEqual(self.store.folder_by_name_key("冬天/精选"), folder)
            self.assertIsNone(self.store.folder_by_name_key("absent"))
        self.assertFalse((self.base / "offline").exists())
        self.assertEqual(set(item.name for item in self.base.iterdir()), {"album.sqlite"})

    def test_name_normalization_uniqueness_and_literal_display_text(self):
        folder = self.create("  Cafe\u0301  ")
        self.assertEqual(folder["name"], "Café")
        self.assertEqual(folder["name_key"], fold_text("CAFÉ"))
        self.assertEqual(folders.normalize_name(" Straße "), ("Straße", "strasse"))
        for duplicate in ("CAFÉ", " cafe\u0301 ", "Café"):
            self.assert_error("FOLDER_NAME_EXISTS", self.create, duplicate)
        self.create("Straße")
        self.assert_error("FOLDER_NAME_EXISTS", self.create, "STRASSE")
        for name in ("../2026", "中文", "literal %_[x]", "<folder>"):
            self.assertEqual(self.create(name)["name"], name)
        for name in ("", "   ", "\nname", "name\t", "bad\x00name", "\x7f", "\u0085", "\u200b", "\ud800",
                     None, 1, [], {}):
            with self.subTest(name=repr(name)):
                self.assert_error("INVALID_ARGUMENT", self.create, name)
        for description in (None, 1, {}, []):
            self.assert_error("INVALID_ARGUMENT", self.create, "valid", description=description)

    def test_list_queries_only_names_and_has_stable_pagination(self):
        created = [self.create(name, description="not-name-query") for name in (
            "Café", "Straße", "中文", "literal %_[x]", "Other")]
        ordered = sorted(folder["folder_id"] for folder in created)
        first = folders.list_folders(store=self.store, limit=2)
        second = folders.list_folders(store=self.store, limit=2, after=first["next_cursor"])
        third = folders.list_folders(store=self.store, limit=2, after=second["next_cursor"])
        self.assertEqual([item["folder_id"] for page in (first, second, third) for item in page["items"]], ordered)
        self.assertIsNone(third["next_cursor"])
        self.assertEqual(first["total"], 5)
        for query, expected in (("CAFE\u0301", "Café"), ("STRASSE", "Straße"), ("中文", "中文"),
                                ("%_[x]", "literal %_[x]")):
            result = folders.list_folders(store=self.store, query=query)
            self.assertEqual([item["name"] for item in result["items"]], [expected])
        self.assertEqual(folders.list_folders(store=self.store, query="not-name-query")["total"], 0)
        self.assertEqual(folders.list_folders(store=self.store, after="zz")["items"], [])
        for arguments in ({"limit": 0}, {"limit": 1001}, {"limit": True}, {"limit": "2"},
                          {"after": None}, {"after": 1}, {"query": ""}, {"query": "  "}, {"query": 4}):
            self.assert_error("INVALID_ARGUMENT", folders.list_folders, store=self.store, **arguments)

    def test_many_to_many_membership_and_delete_only_relationships(self):
        self.configure()
        first, second = self.create("First"), self.create("Second")
        before = self.physical_rows()
        folders.add_photos(first["folder_id"], self.ids[:2], store=self.store)
        folders.add_photos(second["folder_id"], self.ids[:1], store=self.store)
        memberships = self.store.photo_folders(self.ids[0])
        self.assertEqual({item["folder_id"] for item in memberships}, {first["folder_id"], second["folder_id"]})
        self.assertEqual({item["photo_count"] for item in memberships}, {1, 2})
        self.assertEqual(self.store.photo_folders(self.ids[3]), [])
        removed = folders.remove_photos(first["folder_id"], self.ids[:1], store=self.store)
        self.assertEqual(removed["counts"]["removed"], 1)
        self.assertEqual(self.store.folder(second["folder_id"])["photo_count"], 1)
        deleted = folders.delete_folder(first["folder_id"], store=self.store)
        self.assertEqual(deleted["counts"], {"deleted": 1, "removed": 1})
        self.assertEqual(deleted["folder"]["folder_id"], first["folder_id"])
        self.assertEqual(self.store.photo_folders(self.ids[0])[0]["folder_id"], second["folder_id"])
        self.assertEqual(self.physical_rows(), before)
        self.assertEqual(self.create("First")["photo_count"], 0)

    def test_rename_preserves_identity_members_and_unchanged_timestamps(self):
        folder = self.create("First", description="kept")
        folder_id = folder["folder_id"]
        folders.add_photos(folder_id, self.ids[:2], store=self.store)
        before_rows = self.membership_rows()
        updated = self.store.folder(folder_id)
        result = folders.rename_folder(folder_id, "  FIRST  ", store=self.store)
        self.assertEqual(result["counts"], {"renamed": 1, "unchanged": 0})
        self.assertEqual(result["folder"]["folder_id"], folder_id)
        self.assertEqual(result["folder"]["created_at"], folder["created_at"])
        self.assertEqual(result["folder"]["description"], "kept")
        self.assertEqual(result["folder"]["photo_count"], 2)
        self.assertNotEqual(result["folder"]["updated_at"], updated["updated_at"])
        self.assertEqual(self.membership_rows(), before_rows)
        before = self.folder_state()
        result = folders.rename_folder(folder_id, " FIRST ", store=self.store)
        self.assertEqual(result["counts"], {"renamed": 0, "unchanged": 1})
        self.assertEqual(self.folder_state(), before)
        self.create("Second")
        before = self.folder_state()
        self.assert_error("FOLDER_NAME_EXISTS", folders.rename_folder, folder_id, "second", store=self.store)
        self.assertEqual(self.folder_state(), before)

    def test_duplicate_and_empty_selections_report_explicit_noops(self):
        folder_id = self.create()["folder_id"]
        added = folders.add_photos(folder_id, [self.ids[0], self.ids[0], self.ids[1]], store=self.store)
        self.assertEqual(added["counts"], {
            "requested": 3, "unique": 2, "duplicates": 1, "added": 2, "removed": 0, "unchanged": 0})
        before = self.folder_state()
        again = folders.add_photos(folder_id, [self.ids[0], self.ids[0], self.ids[1]], store=self.store)
        self.assertEqual(again["counts"], {
            "requested": 3, "unique": 2, "duplicates": 1, "added": 0, "removed": 0, "unchanged": 2})
        self.assertEqual(self.folder_state(), before)
        absent = folders.remove_photos(folder_id, [self.ids[2], self.ids[2]], store=self.store)
        self.assertEqual(absent["counts"], {
            "requested": 2, "unique": 1, "duplicates": 1, "added": 0, "removed": 0, "unchanged": 1})
        self.assertEqual(self.folder_state(), before)
        for operation in (folders.add_photos, folders.remove_photos):
            empty = operation(folder_id, [], store=self.store)
            self.assertEqual(set(empty["counts"].values()), {0})
            self.assertEqual(empty["photo_ids"], [])
            self.assertEqual(self.folder_state(), before)
        removed = folders.remove_photos(folder_id, [self.ids[0], self.ids[0], self.ids[2]], store=self.store)
        self.assertEqual(removed["counts"], {
            "requested": 3, "unique": 2, "duplicates": 1, "added": 0, "removed": 1, "unchanged": 1})
        self.assertEqual(removed["folder"]["photo_count"], 1)

    def test_unknown_ids_and_invalid_arrays_roll_back_the_whole_batch(self):
        folder_id = self.create()["folder_id"]
        folders.add_photos(folder_id, self.ids[:1], store=self.store)
        before = self.folder_state()
        for operation in (folders.add_photos, folders.remove_photos):
            for ids in ([self.ids[1], "missing"], [self.ids[0], "missing"]):
                self.assert_error("PHOTO_NOT_FOUND", operation, folder_id, ids, store=self.store)
                self.assertEqual(self.folder_state(), before)
            for ids in (None, self.ids[0], tuple(self.ids), {}, [None], [1], [""], ["  "], [[]]):
                self.assert_error("INVALID_ARGUMENT", operation, folder_id, ids, store=self.store)
                self.assertEqual(self.folder_state(), before)
        operations = (
            lambda: folders.show_folder("missing", store=self.store),
            lambda: folders.rename_folder("missing", "name", store=self.store),
            lambda: folders.delete_folder("missing", store=self.store),
            lambda: folders.add_photos("missing", [], store=self.store),
            lambda: folders.remove_photos("missing", [], store=self.store),
            lambda: self.store.folder("missing"),
        )
        for operation in operations:
            self.assert_error("FOLDER_NOT_FOUND", operation)
            self.assertEqual(self.folder_state(), before)
        self.assert_error("PHOTO_NOT_FOUND", self.store.photo_folders, "missing")
        for value in ("", " ", None, 1):
            self.assert_error("INVALID_ARGUMENT", folders.delete_folder, value, store=self.store)

    def test_write_failure_after_partial_mutation_rolls_back_members_and_timestamps(self):
        folder_id = self.create()["folder_id"]
        for function, method in ((folders.add_photos, "_add_folder_photos"),
                                 (folders.remove_photos, "_remove_folder_photos")):
            if method == "_remove_folder_photos":
                folders.add_photos(folder_id, self.ids[:2], store=self.store)
            before = self.folder_state()
            original = getattr(self.store, method)

            def interrupted(target, ids):
                original(target, ids[:1])
                raise RuntimeError("synthetic interruption")

            with patch.object(self.store, method, side_effect=interrupted), self.assertRaisesRegex(
                    RuntimeError, "synthetic interruption"):
                function(folder_id, self.ids[:2], store=self.store)
            self.assertEqual(self.folder_state(), before)
            self.assertFalse(self.store.db.in_transaction)

    def test_nested_operations_rollback_with_the_outer_transaction(self):
        before = self.folder_state()
        with self.assertRaisesRegex(RuntimeError, "outer"):
            with self.store.transaction():
                folder_id = self.create()["folder_id"]
                folders.add_photos(folder_id, self.ids, store=self.store)
                raise RuntimeError("outer")
        self.assertEqual(self.folder_state(), before)

    def test_plain_mutations_never_read_embeddings_thumbnails_or_originals(self):
        self.configure()
        before = self.physical_rows()
        forbidden_reads = []

        def authorize(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ and (table == "thumbnails" or table.startswith("image_embedding_")):
                forbidden_reads.append((table, column))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store.db.set_authorizer(authorize)
        try:
            with patch.object(Path, "open", side_effect=AssertionError("original access")), \
                 patch.object(Path, "stat", side_effect=AssertionError("original stat")):
                folder_id = self.create()["folder_id"]
                folders.add_photos(folder_id, self.ids, store=self.store)
                folders.rename_folder(folder_id, "New", store=self.store)
                folders.remove_photos(folder_id, self.ids[:1], store=self.store)
                folders.delete_folder(folder_id, store=self.store)
        finally:
            self.store.db.set_authorizer(None)
        self.assertEqual(forbidden_reads, [])
        self.assertEqual(self.physical_rows(), before)

    def test_show_without_index_and_with_scope_only_coverage(self):
        folder_id = self.create()["folder_id"]
        folders.add_photos(folder_id, [self.ids[1], self.ids[2]], store=self.store)
        with patch.object(self.store, "thumbnail", side_effect=AssertionError("preview access")):
            shown = folders.show_folder(folder_id, store=self.store)
        self.assertEqual(shown["folder"]["photo_count"], 2)
        self.assertEqual(shown["coverage"], {"total": 2, "not_configured": 2})
        self.assertEqual(shown["embedding_configuration"], "not_configured")
        self.assertIsNone(shown["profile_id"])
        self.configure()
        with patch.object(self.store, "thumbnail", wraps=self.store.thumbnail) as thumbnails, \
             patch.object(self.store, "_embedding_result", wraps=self.store._embedding_result) as results:
            shown = folders.show_folder(folder_id, store=self.store, profile_id=self.profile_id)
        self.assertEqual(shown["coverage"]["total"], 2)
        self.assertEqual(shown["coverage"]["ready"], 1)
        self.assertEqual(shown["coverage"]["missing"], 1)
        self.assertEqual(shown["profile_id"], self.profile_id)
        self.assertEqual(shown["coverage_scope"], "selected_folders")
        self.assertTrue(all(call.kwargs["include_data"] is False for call in thumbnails.call_args_list))
        self.assertEqual({call.args[0]["photo_id"] for call in results.call_args_list}, set(self.ids[1:3]))
        self.assert_error("INDEX_PROFILE_NOT_FOUND", folders.show_folder, folder_id,
                          store=self.store, profile_id="missing")

    def test_read_only_mutations_fail_without_changing_the_file(self):
        folder_id = self.create()["folder_id"]
        folders.add_photos(folder_id, self.ids[:1], store=self.store)
        before = self.path.read_bytes()
        with SQLiteStorage.open(self.path) as store:
            operations = (
                lambda: folders.create_folder("Other", store=store),
                lambda: folders.rename_folder(folder_id, "冬天", store=store),
                lambda: folders.delete_folder(folder_id, store=store),
                lambda: folders.add_photos(folder_id, self.ids[:1], store=store),
                lambda: folders.remove_photos(folder_id, self.ids[1:2], store=store),
                lambda: folders.add_photos(folder_id, [], store=store),
                lambda: folders.remove_photos(folder_id, [], store=store),
            )
            for operation in operations:
                self.assert_error("STORAGE_READ_ONLY", operation)
            self.assertEqual(folders.list_folders(store=store)["total"], 1)
            self.assertEqual(folders.show_folder(folder_id, store=store)["folder"]["photo_count"], 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_locked_mutations_fail_clearly_and_leave_no_partial_changes(self):
        folder_id = self.create()["folder_id"]
        before = self.folder_state()
        self.store.db.execute("PRAGMA busy_timeout=1")
        with SQLiteStorage.open(self.path, writable=True) as contender:
            contender.db.execute("PRAGMA busy_timeout=1")
            with self.store.transaction():
                operations = (
                    lambda: folders.create_folder("Other", store=contender),
                    lambda: folders.rename_folder(folder_id, "New", store=contender),
                    lambda: folders.delete_folder(folder_id, store=contender),
                    lambda: folders.add_photos(folder_id, self.ids, store=contender),
                    lambda: folders.remove_photos(folder_id, self.ids, store=contender),
                )
                for operation in operations:
                    self.assert_error("STORAGE_BUSY", operation)
                    self.assertFalse(contender.db.in_transaction)
            with contender.read_snapshot():
                contender.folder(folder_id)
                self.assert_error("STORAGE_BUSY", folders.add_photos, folder_id, self.ids, store=self.store)
                self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.folder_state(), before)

    def test_schema_foreign_keys_unique_membership_and_reverse_index(self):
        folder_id = self.create()["folder_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("INSERT INTO virtual_folder_photos VALUES (?,?,?)",
                                  (folder_id, "missing", "now"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("INSERT INTO virtual_folder_photos VALUES (?,?,?)",
                                  ("missing", self.ids[0], "now"))
        folders.add_photos(folder_id, self.ids[:1], store=self.store)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("INSERT INTO virtual_folder_photos VALUES (?,?,?)",
                                  (folder_id, self.ids[0], "now"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("INSERT INTO virtual_folders SELECT 'duplicate',name,name_key,description,"
                                  "created_at,updated_at FROM virtual_folders")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("DELETE FROM photos WHERE photo_id=?", (self.ids[0],))
        index_columns = [row["name"] for row in self.store.db.execute(
            "PRAGMA index_info(virtual_folder_photos_photo)")]
        self.assertEqual(index_columns, ["photo_id", "folder_id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._add_folder_photos(folder_id, [self.ids[1], "missing"])
        self.assertEqual(self.store.folder(folder_id)["photo_count"], 1)
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_members_survive_reopen_backup_move_and_photo_profile_changes(self):
        self.configure()
        folder_id = self.create()["folder_id"]
        folders.add_photos(folder_id, self.ids[:2], store=self.store)
        original = self.store.photo(self.ids[0])
        self.store.put_photo({**original, "original_absolute_path": "Z:\\offline\\relocated.jpg",
                              "content_version": "changed-content", "metadata": {"exif": {"datetime_original": "changed"}}})
        other_profile_id = self.store.put_embedding_profile({**self.profile, "model": "other-model"})
        self.store.set_default_embedding_profile(other_profile_id)
        self.store.put_photo({**original, "photo_id": "new-after-organization"})
        self.assertEqual(self.store.photo_folders("new-after-organization"), [])
        expected = self.folder_state()
        album_id = self.store.album()["id"]
        self.store.close()
        backup = self.base / "backup.sqlite"
        with SQLiteStorage.open(self.path) as source:
            self.assertEqual(self.folder_state(source), expected)
            source.backup(backup)
        moved = self.base / "moved.sqlite"
        backup.rename(moved)
        with SQLiteStorage.open(moved) as store:
            self.assertEqual(store.album()["id"], album_id)
            self.assertEqual(self.folder_state(store), expected)
            self.assertEqual(store.folder(folder_id)["photo_count"], 2)
            self.assertEqual(store.default_embedding_profile(), other_profile_id)

    def test_scope_album_single_union_intersection_dedup_and_empty(self):
        first, second, empty = self.create("First"), self.create("Second"), self.create("Empty")
        folders.add_photos(first["folder_id"], self.ids[:2], store=self.store)
        folders.add_photos(second["folder_id"], self.ids[1:3], store=self.store)
        cases = (
            (None, None, self.ids),
            ([], None, self.ids),
            ([first["folder_id"]], None, self.ids[:2]),
            ([first["folder_id"], first["folder_id"]], None, self.ids[:2]),
            ([first["folder_id"], second["folder_id"]], "union", self.ids[:3]),
            ([second["folder_id"], first["folder_id"]], "intersection", self.ids[1:2]),
            ([first["folder_id"], second["folder_id"], first["folder_id"]], "intersection", self.ids[1:2]),
            ([empty["folder_id"]], None, []),
            ([first["folder_id"], empty["folder_id"]], "intersection", []),
            ([first["folder_id"], empty["folder_id"]], "union", self.ids[:2]),
        )
        with patch.object(self.store, "thumbnail", side_effect=AssertionError("preview access")), \
             patch.object(self.store, "_embedding_result", side_effect=AssertionError("embedding access")):
            for ids, match, expected in cases:
                with self.subTest(ids=ids, match=match), self.store.read_snapshot():
                    scope, members = folders.resolve_scope(store=self.store, folder_ids=ids, folder_match=match)
                self.assertEqual([photo["photo_id"] for photo in members], expected)
                if not ids:
                    self.assertEqual(scope, {"kind": "album"})
                else:
                    self.assertEqual(scope["kind"], "virtual_folders")
                    self.assertEqual(scope["match"], match or "union")
                    self.assertEqual([folder["folder_id"] for folder in scope["folders"]], sorted(set(ids)))
                    self.assertTrue(all(set(folder) == {"folder_id", "name"} for folder in scope["folders"]))
        self.assertEqual([photo["photo_id"] for photo in self.store.photos()], self.ids)
        self.assertEqual(self.store.photos_in_folders([], "union"), [])

    def test_scope_invalid_selections_never_fall_back_to_album(self):
        first, second = self.create("First"), self.create("Second")
        cases = (
            (None, "union"), ([], "intersection"), ([first["folder_id"]], "invalid"),
            ([first["folder_id"], second["folder_id"]], None),
            ("not-an-array", None), ((first["folder_id"],), None),
            ([None], None), ([""], None), (["  "], None), ([1], None), ({}, None),
        )
        for ids, match in cases:
            self.assert_error("INVALID_ARGUMENT", folders.resolve_scope,
                              store=self.store, folder_ids=ids, folder_match=match)
        self.assert_error("FOLDER_NOT_FOUND", folders.resolve_scope, store=self.store, folder_ids=["missing"])
        self.assert_error("FOLDER_NOT_FOUND", folders.resolve_scope, store=self.store,
                          folder_ids=[first["folder_id"], "missing"], folder_match="union")
        self.assert_error("INVALID_ARGUMENT", self.store.photos_in_folders, [first["folder_id"]], "invalid")

    def test_scope_is_sql_prefiltered_before_photo_materialization(self):
        folder_id = self.create()["folder_id"]
        folders.add_photos(folder_id, self.ids[1:2], store=self.store)
        with patch.object(self.store, "_photo", wraps=self.store._photo) as decode, \
             patch.object(self.store, "photos", side_effect=AssertionError("whole album materialized")):
            scope, members = folders.resolve_scope(store=self.store, folder_ids=[folder_id])
        self.assertEqual(scope["kind"], "virtual_folders")
        self.assertEqual([photo["photo_id"] for photo in members], self.ids[1:2])
        self.assertEqual([call.args[0]["photo_id"] for call in decode.call_args_list], self.ids[1:2])

    def test_search_add_validates_in_write_transaction_without_reencoding_or_pixels(self):
        self.configure()
        source, target = self.create("Source"), self.create("Target")
        folders.add_photos(source["folder_id"], self.ids[:2], store=self.store)
        snapshot = self.search(folder_ids=[source["folder_id"]])
        snapshot["pixels"] = "UNTRUSTED_IMAGE_PAYLOAD"
        snapshot["results"][0]["metadata"] = {"private": "UNTRUSTED_IMAGE_PAYLOAD"}
        snapshot["scope"]["folders"][0]["extra_image"] = "UNTRUSTED_IMAGE_PAYLOAD"
        original = management.select_search_results

        def checked(*args, **kwargs):
            self.assertTrue(self.store.db.in_transaction)
            return original(*args, **kwargs)

        with patch.object(management, "select_search_results", side_effect=checked) as select, \
             patch.object(self.store, "thumbnail", wraps=self.store.thumbnail) as thumbnails, \
             patch.object(Path, "open", side_effect=AssertionError("original access")), \
             patch.object(Path, "stat", side_effect=AssertionError("original stat")):
            result = folders.add_photos(target["folder_id"], [self.ids[1], self.ids[1]], store=self.store,
                                        search_snapshot=snapshot)
        self.assertEqual(select.call_count, 1)
        self.assertEqual(len(self.encoder.queries), 1)
        self.assertTrue(all(call.kwargs["include_data"] is False for call in thumbnails.call_args_list))
        self.assertEqual(result["counts"]["added"], 1)
        self.assertEqual(result["counts"]["duplicates"], 1)
        self.assertEqual(result["selection"]["photo_ids"], self.ids[1:2])
        self.assertEqual(result["source_search"]["snapshot_id"], snapshot["snapshot_id"])
        self.assertEqual(result["source_search"]["scope"]["folders"],
                         [{"folder_id": source["folder_id"], "name": source["name"]}])
        self.assertEqual(result["source_search"]["coverage"]["total"], 2)
        self.assertEqual(result["source_search"]["coverage_scope"], "selected_folders")
        self.assertNotIn("UNTRUSTED_IMAGE_PAYLOAD", json.dumps(result))
        self.assertNotIn("results", result)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["image_model_calls"], 0)
        self.assertEqual(self.store.folder(source["folder_id"])["photo_count"], 2)

    def test_search_add_accepts_historical_scope_but_rejects_stale_selected_inputs(self):
        self.configure()
        source, target = self.create("Source"), self.create("Target")
        folders.add_photos(source["folder_id"], self.ids[:2], store=self.store)
        snapshot = self.search(folder_ids=[source["folder_id"]])
        folders.rename_folder(source["folder_id"], "Renamed", store=self.store)
        folders.delete_folder(source["folder_id"], store=self.store)
        result = folders.add_photos(target["folder_id"], self.ids[:1], store=self.store, search_snapshot=snapshot)
        self.assertEqual(result["source_search"]["scope"]["folders"][0]["name"], "Source")
        self.assertEqual(result["counts"]["added"], 1)
        changed = self.store.photo(self.ids[1])
        self.store.put_photo({**changed, "content_version": "changed"})
        before = self.folder_state()
        self.assert_error("SEARCH_SNAPSHOT_STALE", folders.add_photos, target["folder_id"], self.ids[:2],
                          store=self.store, search_snapshot=snapshot)
        self.assertEqual(self.folder_state(), before)
        self.assertEqual(len(self.encoder.queries), 1)

    def test_search_add_rejects_non_candidates_invalid_snapshots_and_cross_album(self):
        self.configure()
        target = self.create()
        snapshot = self.search(limit=1)
        before = self.folder_state()
        self.assert_error("INVALID_ARGUMENT", folders.add_photos, target["folder_id"], self.ids[:2],
                          store=self.store, search_snapshot=snapshot)
        foreign = deepcopy(snapshot)
        foreign["album"]["id"] = str(uuid4())
        self.assert_error("ALBUM_MISMATCH", folders.add_photos, target["folder_id"], self.ids[:1],
                          store=self.store, search_snapshot=foreign)
        for invalid in ({}, [], {**snapshot, "schema": "album-snapshot-v1"},
                        {**snapshot, "display_stage": "selected"}, {**snapshot, "mode": "metadata"}):
            self.assert_error("INVALID_ARGUMENT", folders.add_photos, target["folder_id"], self.ids[:1],
                              store=self.store, search_snapshot=invalid)
        self.assertEqual(self.folder_state(), before)

    def test_search_add_empty_selection_is_validated_and_never_all_candidates(self):
        self.configure()
        target = self.create()
        snapshot = self.search()
        before = self.folder_state()
        with patch.object(management, "_check_candidate", side_effect=AssertionError("selected no inputs")):
            result = folders.add_photos(target["folder_id"], [], store=self.store, search_snapshot=snapshot)
        self.assertEqual(set(result["counts"].values()), {0})
        self.assertEqual(result["selection"]["selected_count"], 0)
        self.assertEqual(result["selection"]["not_selected_count"], 2)
        self.assertEqual(self.folder_state(), before)
        self.assert_error("INVALID_ARGUMENT", folders.add_photos, target["folder_id"], [],
                          store=self.store, search_snapshot={})

    def test_search_add_rejects_changed_selected_vectors_atomically(self):
        self.configure()
        target = self.create()
        snapshot = self.search()
        self.store.db.execute("UPDATE image_embedding_results SET vector=? WHERE photo_id=?",
                              (b"invalid vector", self.ids[1]))
        before = self.folder_state()
        self.assert_error("SEARCH_SNAPSHOT_STALE", folders.add_photos, target["folder_id"], self.ids[:2],
                          store=self.store, search_snapshot=snapshot)
        self.assertEqual(self.folder_state(), before)


if __name__ == "__main__":
    unittest.main()
