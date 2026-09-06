from __future__ import annotations

import argparse
import builtins
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from photography_lib import management, management_cli, source_paths
from photography_lib.config import Config, PhotographyError
from photography_lib.exports import export_path
from photography_lib.image_embedding_profiles import default_model_dir
from photography_lib.image_vectors import pack_vector
from photography_lib.management_report import management_report
from photography_lib.sqlite_storage import SQLiteStorage


class FakeEncoder:
    def __init__(self, profile, vector=(1.0, 0.0, 0.0), on_encode=None):
        self.identity = deepcopy(profile)
        self.vector = vector
        self.queries = []
        self.on_encode = on_encode

    def profile(self):
        return deepcopy(self.identity)

    def encode_text(self, text):
        self.queries.append(text)
        if self.on_encode:
            self.on_encode()
        return argparse.Namespace(vector=self.vector, elapsed_seconds=0.001, token_count=5)

    def encode_image(self, data):
        raise AssertionError("Management must never encode an image.")


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / (".management-fixture-" + uuid4().hex)
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base)
        self.config = Config(self.base / "旅行.sqlite", model_cache_root=self.base / "models", thumbnail_size=64)
        self.store = SQLiteStorage.create(self.config.database_path)
        self.addCleanup(self.store.close)
        self.source = self.base / "offline-originals"
        self.profile = {
            "profile_schema": "image-embedding-profile-v1", "embedding_kind": "image_text_semantic",
            "stored_modality": "image", "input_scope": "stored_thumbnail", "granularity": "whole_image",
            "model": "offline-test", "dimensions": 3, "dtype": "float32-le", "normalized": True,
            "image": {"feature_api": "offline-image"}, "text": {"feature_api": "offline-text"},
        }
        self.profile_id = self.store.put_embedding_profile(self.profile)
        self.other_profile = {**self.profile, "model": "another-space"}
        self.other_profile_id = self.store.put_embedding_profile(self.other_profile)
        names = ("旅行/Cafe\u0301.JPG", "Trips/STRASSE.JPG", "literal/%_[x].jpg",
                 "Trips/中文街道.jpg", "Other/no-embedding.jpg", "Other/broken.jpg")
        self.ids = []
        self.previews = {}
        for i, name in enumerate(names, 1):
            photo_id = f"p{i:02d}"
            self.ids.append(photo_id)
            data = self.jpeg((i * 20, 30, 40))
            self.previews[photo_id] = data
            photo = {
                "photo_id": photo_id,
                "original_absolute_path": str(self.source.joinpath(*name.split("/"))),
                "original_relative_path": self.source.name + "/" + name,
                "content_version": hashlib.sha256(data).hexdigest(),
                "thumbnail_profile": self.config.thumbnail_profile,
                "size_bytes": len(data), "mtime_ns": 1,
                "metadata": {"width": 32, "height": 24, "format": "JPEG"},
                "ingest_state": "available", "original_status": "not_checked",
                "last_ingest_error": None, "last_path_error": None, "last_original_check": None,
                "created_at": "2026-09-06T00:00:00Z", "updated_at": "2026-09-06T00:00:00Z",
                "path_updated_at": None,
            }
            self.store.put_photo(photo)
            self.store.put_thumbnail(photo, data)
        original_import = builtins.__import__

        def guard(name, *args, **kwargs):
            if name.split(".")[0] in ("torch", "transformers", "numpy", "huggingface_hub", "tokenizers"):
                raise AssertionError("No optional inference runtime may be imported in these offline tests.")
            return original_import(name, *args, **kwargs)

        import_guard = patch("builtins.__import__", side_effect=guard)
        import_guard.start()
        self.addCleanup(import_guard.stop)

    @staticmethod
    def jpeg(color):
        buffer = io.BytesIO()
        Image.new("RGB", (32, 24), color).save(buffer, format="JPEG")
        return buffer.getvalue()

    def seed(self, photo_id, vector=(1.0, 0.0, 0.0), profile_id=None):
        photo = self.store.photo(photo_id)
        snapshot = {**photo, "input_image_hash": self.store.thumbnail(photo_id, include_data=False)["image_hash"]}
        blob = pack_vector(vector, 3)
        return self.store._put_embedding_result(snapshot, profile_id or self.profile_id, blob,
                                                hashlib.sha256(blob).hexdigest(), 3)

    def configure(self):
        self.store.set_default_embedding_profile(self.profile_id)

    def parse(self, *args):
        parser = argparse.ArgumentParser()
        management_cli.add_commands(parser.add_subparsers(dest="command", required=True))
        return parser.parse_args(["management", *args])

    def command(self, *args):
        return management_cli.command(self.parse(*args), self.store, self.config)

    def search(self, query="街上的人", **kwargs):
        kwargs.setdefault("encoder", FakeEncoder(self.profile))
        return management.semantic_search(query, store=self.store, config=self.config, **kwargs)

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(PhotographyError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def materialize(self, photo_id=None):
        photo_id = photo_id or self.ids[0]
        path = Path(self.store.photo(photo_id)["original_absolute_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.previews[photo_id])
        return path

    def report(self, snapshot, name="view.html"):
        output = self.base / name
        management_report(snapshot, output, config=self.config, store=self.store)
        return output.read_text(encoding="utf-8")

    def test_create_and_open_describe_one_album_without_loading_models(self):
        for action in ("create", "open"):
            result = self.command(action)
            self.assertEqual(result["schema"], "album-snapshot-v1")
            self.assertEqual(result["view"], action)
            self.assertEqual(result["album"]["database_path"], str(self.config.database_path))
            self.assertEqual(result["album"]["name"], self.config.database_path.stem)
            self.assertEqual(str(UUID(result["album"]["id"])), result["album"]["id"])
            self.assertEqual(result["photo_count"], 6)
            self.assertEqual(result["coverage"], {"total": 6, "not_configured": 6})
            self.assertEqual(result["model_calls"], 0)
        self.configure()
        self.seed(self.ids[0])
        self.assertEqual(self.command("open")["coverage"]["ready"], 1)
        self.assertFalse(self.config.model_cache_root.exists())

    def test_browse_without_default_never_reads_preview_blob(self):
        with patch.object(self.store, "thumbnail", wraps=self.store.thumbnail) as thumbnail:
            page = management.photos(store=self.store)
            single = management.photo(self.ids[0], store=self.store)
            self.assertTrue(all(call.kwargs.get("include_data") is False for call in thumbnail.call_args_list))
        self.assertEqual(page["embedding_configuration"], "not_configured")
        self.assertIsNone(page["profile_id"])
        self.assertEqual(page["limit"], 100)
        self.assertEqual(len(page["items"]), 6)
        self.assertEqual(single["items"][0]["image_embedding"]["status"], "not_configured")
        self.assertEqual(single["items"][0]["preview_integrity"], "unchecked")
        self.assertEqual(single["items"][0]["original_verification"], "not_checked")
        self.assertEqual(page["album"], self.store.album())
        self.assertNotIn("scope", page)
        self.assertNotIn("target", page)
        self.assertFalse(self.source.exists())

    def test_photo_pagination_is_stable_id_order(self):
        pages, after = [], ""
        for _ in range(3):
            page = management.photos(store=self.store, limit=2, after=after)
            pages.extend(page["items"])
            after = page["next_cursor"]
        self.assertEqual([p["photo_id"] for p in pages], self.ids)
        self.assertIsNone(after)
        self.assertEqual(management.photos(store=self.store, after="zz")["items"], [])

    def test_metadata_normalization_casefold_and_literal_substrings(self):
        for query, expected in (
            ("CAFÉ", self.ids[0]), ("CAFE\u0301", self.ids[0]), ("straße", self.ids[1]),
            ("%_[x]", self.ids[2]), ("中文", self.ids[3]), ("旅行/Café", self.ids[0]),
        ):
            with self.subTest(query=query):
                result = management.metadata_search(query, store=self.store)
                self.assertEqual([p["photo_id"] for p in result["items"]], [expected])
                self.assertEqual(result["model_calls"], 0)
        for query in (".*", "does-not-exist"):
            self.assertEqual(management.metadata_search(query, store=self.store)["total"], 0)

    def test_metadata_checks_both_saved_paths_but_not_metadata_descriptions(self):
        photo = self.store.photo(self.ids[-1])
        self.store.put_photo({**photo, "original_absolute_path": str(self.base / "independent-filename.jpg"),
                              "metadata": {**photo["metadata"], "description": "must-not-match"}})
        self.assertEqual(management.metadata_search("independent-filename", store=self.store)["total"], 1)
        self.assertEqual(management.metadata_search("Other/broken", store=self.store)["total"], 1)
        self.assertEqual(management.metadata_search("must-not-match", store=self.store)["total"], 0)
        first = management.metadata_search("jpg", store=self.store, limit=1)
        second = management.metadata_search("jpg", store=self.store, limit=1, after=first["next_cursor"])
        self.assertEqual(first["total"], 6)
        self.assertEqual(second["items"][0]["photo_id"], self.ids[1])

    def test_explicit_profile_does_not_switch_the_saved_default(self):
        self.configure()
        self.seed(self.ids[0])
        current = management.photo(self.ids[0], store=self.store)
        other = management.photo(self.ids[0], store=self.store, profile_id=self.other_profile_id)
        self.assertEqual(current["items"][0]["image_embedding"]["status"], "ready")
        self.assertEqual(other["items"][0]["image_embedding"]["status"], "missing")
        self.assertEqual(other["profile_id"], self.other_profile_id)
        self.assertEqual(self.store.default_embedding_profile(), self.profile_id)

    def test_parameter_validation_and_missing_photo(self):
        for limit in (0, -1, 1001, True, 1.5, "2"):
            with self.subTest(limit=limit):
                self.assert_error("INVALID_ARGUMENT", management.photos, store=self.store, limit=limit)
                self.assert_error("INVALID_ARGUMENT", self.search, limit=limit)
        for query in ("", " \t\n"):
            self.assert_error("INVALID_ARGUMENT", management.metadata_search, query, store=self.store)
            self.assert_error("INVALID_ARGUMENT", self.search, query)
        self.assert_error("INVALID_ARGUMENT", management.photos, store=self.store, after=3)
        for after in ("", "p01"):
            self.assert_error("INVALID_ARGUMENT", self.search, after=after)
        self.assert_error("PHOTO_NOT_FOUND", management.photo, "absent", store=self.store)

    def test_semantic_requires_profile_and_no_candidates_skips_encoder(self):
        encoder = FakeEncoder(self.profile)
        self.assert_error("INDEX_CONFIGURATION_REQUIRED", self.search, encoder=encoder)
        result = self.search(profile_id=self.profile_id, encoder=encoder)
        self.assertEqual(result["coverage"], {"total": 6, "ready": 0, "missing": 6, "stale": 0,
                                             "invalid_input": 0, "invalid_vector": 0})
        self.assertEqual(result["results"], [])
        self.assertEqual(result["model_calls"], 0)
        self.assertIsNone(result["timings"]["query_encoding_seconds"])
        self.assertEqual(encoder.queries, [])
        self.assertIsNone(self.store.default_embedding_profile())
        without_runtime = management.semantic_search("query", store=self.store, profile_id=self.profile_id)
        self.assertEqual(without_runtime["results"], [])

    def test_semantic_default_exact_cosine_and_ties(self):
        self.configure()
        first_id = self.seed(self.ids[0])
        second_id = self.seed(self.ids[1])
        self.seed(self.ids[2], (0.6, 0.8, 0.0))
        self.seed(self.ids[3], (-1.0, 0.0, 0.0))
        encoder = FakeEncoder(self.profile, on_encode=lambda: self.assertFalse(self.store.db.in_transaction))
        result = self.search(encoder=encoder)
        self.assertEqual(result["limit"], 10)
        self.assertEqual(encoder.queries, ["街上的人"])
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["image_model_calls"], 0)
        self.assertEqual(result["component"], "image_embedding")
        self.assertEqual([p["photo_id"] for p in result["results"]], self.ids[:4])
        self.assertEqual([p["result_id"] for p in result["results"][:2]], [first_id, second_id])
        self.assertEqual([p["score"] for p in result["results"][:2]], [1.0, 1.0])
        self.assertAlmostEqual(result["results"][2]["score"], 0.6, places=6)
        self.assertEqual(result["results"][3]["score"], -1.0)
        for item in result["results"]:
            self.assertEqual(item["profile_id"], self.profile_id)
            self.assertEqual(item["input_image_hash"], self.store.thumbnail(item["photo_id"], include_data=False)["image_hash"])
            self.assertNotIn("vector", item)
            self.assertNotIn("description", item)

    def test_cosine_divides_by_actual_norm_not_just_dot_product(self):
        self.configure()
        self.seed(self.ids[0], (1.0, 0.0, 0.0))
        self.seed(self.ids[1], (1.00001, 0.0, 0.0))
        result = self.search()
        self.assertEqual([p["photo_id"] for p in result["results"]], self.ids[:2])
        self.assertEqual([p["score"] for p in result["results"]], [1.0, 1.0])

    def test_coverage_is_entire_album_not_top_k_or_other_profiles(self):
        self.configure()
        self.seed(self.ids[0])
        self.seed(self.ids[1])
        self.seed(self.ids[2], profile_id=self.other_profile_id)
        self.seed(self.ids[3])
        changed = {**self.store.photo(self.ids[3]), "content_version": hashlib.sha256(b"changed").hexdigest()}
        self.store.put_photo(changed)
        self.store.put_thumbnail(changed, self.jpeg((9, 8, 7)))
        self.store.put_photo({**self.store.photo(self.ids[4]), "ingest_state": "error"})
        damaged_id = self.seed(self.ids[5])
        self.store.db.execute("UPDATE image_embedding_results SET vector_hash=? WHERE result_id=?", ("damaged", damaged_id))
        result = self.search(limit=1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["coverage"], {"total": 6, "ready": 2, "missing": 1, "stale": 1,
                                             "invalid_input": 1, "invalid_vector": 1})
        self.assertEqual(result["coverage_scope"], "entire_album")
        self.assertEqual(len(result["coverage_items"]), 6)
        alternate = self.search(profile_id=self.other_profile_id, encoder=FakeEncoder(self.other_profile))
        self.assertEqual([p["photo_id"] for p in alternate["results"]], [self.ids[2]])
        self.assertEqual(self.store.default_embedding_profile(), self.profile_id)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_embedding_results").fetchone()[0], 5)

    def test_profile_mismatch_and_invalid_query_vectors_fail_closed(self):
        self.configure()
        self.seed(self.ids[0])
        wrong = FakeEncoder(self.other_profile)
        self.assert_error("INDEX_PROFILE_MISMATCH", self.search, encoder=wrong)
        self.assertEqual(wrong.queries, [])
        for vector in ((1.0,), (0.0, 0.0, 0.0), (float("nan"), 0.0, 0.0),
                       (float("inf"), 0.0, 0.0), (2.0, 0.0, 0.0)):
            with self.subTest(vector=vector), self.assertRaises(PhotographyError):
                self.search(encoder=FakeEncoder(self.profile, vector=vector))

    def test_snapshot_is_consistent_across_concurrent_photo_update(self):
        self.configure()
        result_id = self.seed(self.ids[0])
        old_version = self.store.photo(self.ids[0])["content_version"]
        self.store.db.execute("PRAGMA journal_mode=WAL")
        with SQLiteStorage.open(self.config.database_path, writable=True) as writer:
            original_select = self.store.photos

            def change_after_selection():
                photos = original_select()
                changed = {**writer.photo(self.ids[0]), "content_version": hashlib.sha256(b"new").hexdigest()}
                with writer.transaction():
                    writer.put_photo(changed)
                    writer.put_thumbnail(changed, self.jpeg((200, 10, 20)))
                return photos

            with patch.object(self.store, "photos", side_effect=change_after_selection):
                result = self.search()
        self.assertEqual(result["coverage"]["ready"], 1)
        self.assertEqual(result["results"][0]["result_id"], result_id)
        self.assertEqual(result["results"][0]["content_version"], old_version)

    def test_readonly_browse_and_search_never_access_originals_or_write(self):
        self.configure()
        self.seed(self.ids[0])
        for photo_id in self.ids:
            self.store.put_photo({**self.store.photo(photo_id), "original_status": "missing"})
        original_stat, original_open = Path.stat, Path.open

        def guard_stat(path, *args, **kwargs):
            if path.is_relative_to(self.source):
                raise AssertionError("Browsing must not stat originals.")
            return original_stat(path, *args, **kwargs)

        def guard_open(path, *args, **kwargs):
            if path.is_relative_to(self.source):
                raise AssertionError("Browsing must not open originals.")
            return original_open(path, *args, **kwargs)

        with SQLiteStorage.open(self.config.database_path) as reader:
            with patch.object(Path, "stat", guard_stat), patch.object(Path, "open", guard_open):
                for function, arguments in (
                    (management.photos, ()), (management.photo, (self.ids[0],)),
                    (management.metadata_search, ("café",)), (management.album_info, ()),
                ):
                    self.assertEqual(function(*arguments, store=reader)["album"]["id"], self.store.album()["id"])
                result = management.semantic_search("query", store=reader, encoder=FakeEncoder(self.profile))
                self.assertEqual(result["results"][0]["photo_id"], self.ids[0])
            self.assertEqual(reader.db.total_changes, 0)

    def test_two_album_files_are_isolated_even_with_identical_photo_ids(self):
        self.configure()
        self.seed(self.ids[0])
        with SQLiteStorage.create(self.base / "other.sqlite") as other:
            other.put_photo(self.store.photo(self.ids[0]))
            other.put_thumbnail(other.photo(self.ids[0]), self.previews[self.ids[0]])
            result = management.photos(store=other)
            self.assertNotEqual(result["album"]["id"], self.store.album()["id"])
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["image_embedding"]["status"], "not_configured")
            self.assert_error("INDEX_CONFIGURATION_REQUIRED", management.semantic_search, "x", store=other)

    def test_cli_contract_defaults_and_removed_options(self):
        self.assertEqual(self.command("photo", self.ids[0])["view"], "photo")
        self.assertEqual(self.command("photos")["limit"], 100)
        self.assertEqual(self.command("search", "jpg", "--mode", "metadata")["limit"], 100)
        result = self.command("search", "anything", "--mode", "semantic", "--profile-id", self.profile_id)
        self.assertEqual(result["limit"], 10)
        self.assert_error("INVALID_ARGUMENT", self.command, "search", "x", "--mode", "semantic", "--after", "")
        for arguments in (
            ("albums",), ("album", "x"), ("album-create", "x"), ("search", "query"),
            ("photos", "--album-id", "a"), ("photos", "--library-id", "b"),
            ("search", "x", "--mode", "metadata", "--target", "photos"),
            ("search", "x", "--mode", "semantic", "--model-dir", "x"),
            ("relink", self.ids[0]),
        ):
            with self.subTest(arguments=arguments), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                self.parse(*arguments)

    def test_semantic_uses_global_cache_root_without_creating_it(self):
        self.configure()
        self.seed(self.ids[0])
        encoder = FakeEncoder(self.profile)
        with patch("photography_lib.siglip_embedding.SiglipEncoder", return_value=encoder) as constructor:
            result = self.command("search", "query", "--mode", "semantic")
        constructor.assert_called_once_with(default_model_dir(self.config.model_cache_root), profile=self.profile)
        self.assertEqual(result["profile_id"], self.profile_id)
        self.assertEqual(encoder.queries, ["query"])
        self.assertFalse(self.config.model_cache_root.exists())

    def test_exports_validate_all_targets_before_search_or_encoding(self):
        valid = self.base / "report.json"
        for output in (self.config.model_cache_root / "report.html", self.base / "bad.txt"):
            with self.subTest(output=output), patch.object(management, "semantic_search",
                    side_effect=AssertionError("Export validation must precede search")):
                self.assert_error("INVALID_ARGUMENT", self.command, "search", "query", "--mode", "semantic",
                                  "--output", str(valid), "--html", str(output))
            self.assertFalse(valid.exists())

    def test_json_and_html_exports_beside_database_preserve_readonly_state(self):
        before = self.store.db.total_changes
        output, report = self.base / "view.json", self.base / "view.html"
        result = self.command("photos", "--output", str(output), "--html", str(report))
        self.assertEqual(result["html_output"], str(report))
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result)
        self.assertIn("data:image/jpeg;base64", report.read_text(encoding="utf-8"))
        self.assertEqual(self.store.db.total_changes, before)

    def test_export_protects_exact_database_sidecars_cache_and_original_candidates(self):
        photo = self.store.photo(self.ids[0])
        targets = [self.config.database_path, self.config.model_cache_root / "report.html",
                   Path(photo["original_absolute_path"]), self.base / photo["original_relative_path"]]
        targets.extend(Path(str(self.config.database_path) + suffix) for suffix in
                       ("-journal", "-wal", "-shm", ".image-embedding.lock"))
        for target in targets:
            with self.subTest(target=target):
                self.assert_error("INVALID_ARGUMENT", export_path, target, self.config, self.store,
                                  (target.suffix.lower(),))
        changed = {**photo, "original_absolute_path": str(self.base / "elsewhere.jpg"),
                   "original_relative_path": "protected-relative.html"}
        self.store.put_photo(changed)
        self.assert_error("INVALID_ARGUMENT", export_path, self.base / "protected-relative.html",
                          self.config, self.store, (".html",))
        self.assertEqual(export_path(self.source / "allowed.html", self.config, self.store, (".html",)),
                         self.source / "allowed.html")

    def test_export_rejects_hardlink_aliases_of_database_original_and_cache(self):
        original = self.materialize()
        cache_file = self.config.model_cache_root / "weights"
        cache_file.parent.mkdir()
        cache_file.write_bytes(b"synthetic-model-placeholder")
        for source in (self.config.database_path, original, cache_file):
            target = self.base / (uuid4().hex + ".html")
            os.link(source, target)
            self.assert_error("INVALID_ARGUMENT", export_path, target, self.config, self.store, (".html",))
            target.unlink()

    def test_export_rejects_symlink_aliases(self):
        original = self.materialize()
        alias = self.base / "alias.jpg"
        try:
            alias.symlink_to(original)
        except OSError as exc:
            self.skipTest("Local symbolic links unavailable: " + str(exc))
        self.assert_error("INVALID_ARGUMENT", export_path, alias, self.config, self.store, (".jpg",))

    def test_jpeg_exports_reject_absolute_and_relocated_scan_source_directories(self):
        self.store.start_scan("scan-export", str(self.source), "relocated-source")
        for directory in (self.source, self.source / "nested", self.base / "relocated-source"):
            self.assert_error("INVALID_ARGUMENT", self.command, "thumbnail", self.ids[0],
                              "--output", str(directory / "preview.jpg"))
            self.assertFalse((directory / "preview.jpg").exists())
        output = self.base / "preview.jpg"
        result = self.command("thumbnail", self.ids[0], "--output", str(output))
        self.assertEqual(result["album"], self.store.album())
        self.assertEqual(output.read_bytes(), self.previews[self.ids[0]])
        self.assertEqual(result["model_calls"], 0)

    def test_reports_escape_text_are_chinese_and_have_no_selection_widgets(self):
        self.configure()
        self.seed(self.ids[0])
        result = self.search('<script>alert("query")</script>')
        result["results"][0]["original_relative_path"] = "</pre><img src=x onerror=alert(1)>"
        before = deepcopy(result)
        page = self.report(result)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;/pre&gt;&lt;img", page)
        self.assertNotIn("<script", page)
        self.assertNotIn("<img src=x", page)
        for forbidden in ("<input", "<textarea", "<button", "<form", "search-add", "selected-photo-ids"):
            self.assertNotIn(forbidden, page)
        self.assertIn('lang="zh-CN"', page)
        self.assertIn("相册文件", page)
        self.assertIn("余弦相似度不是概率", page)
        self.assertIn("image_embedding", page)
        self.assertIn("data:image/jpeg;base64", page)
        self.assertEqual(before, result)

    def test_report_rejects_different_album_uuid_before_reading_preview(self):
        snapshot = management.photo(self.ids[0], store=self.store)
        with SQLiteStorage.create(self.base / "other.sqlite") as other:
            with patch.object(other, "thumbnail", side_effect=AssertionError("Wrong album preview must not be read")):
                self.assert_error("ALBUM_MISMATCH", management_report, snapshot, self.base / "wrong.html",
                                  config=self.config, store=other)
        self.assertFalse((self.base / "wrong.html").exists())

    def test_report_accepts_same_album_uuid_at_a_backup_path(self):
        snapshot = management.photo(self.ids[0], store=self.store)
        backup_path = self.base / "moved.sqlite"
        self.command("backup", "--output", str(backup_path))
        with SQLiteStorage.open(backup_path) as backup:
            output = self.base / "from-backup.html"
            management_report(snapshot, output, config=self.config, store=backup)
        self.assertIn('src="data:image/jpeg;base64,', output.read_text(encoding="utf-8"))

    def test_report_does_not_stat_or_open_original_files(self):
        snapshot = management.photos(store=self.store)
        original_stat, original_open = Path.stat, Path.open

        def guard_stat(path, *args, **kwargs):
            if path != self.source and path.is_relative_to(self.source):
                raise AssertionError("Report must not stat original files.")
            return original_stat(path, *args, **kwargs)

        def guard_open(path, *args, **kwargs):
            if path.is_relative_to(self.source):
                raise AssertionError("Report must not open original files.")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "stat", guard_stat), patch.object(Path, "open", guard_open):
            page = self.report(snapshot)
        self.assertEqual(page.count('src="data:image/jpeg;base64,'), 6)

    def test_report_validates_album_uuid_and_rejects_legacy_schema(self):
        snapshot = management.photos(store=self.store)
        for value in (None, {}, {"id": "not-a-uuid"}, {"id": 12}):
            self.assert_error("INVALID_ARGUMENT", management_report, {**snapshot, "album": value},
                              self.base / "bad.html", config=self.config, store=self.store)
        for schema in ("management-snapshot-v1", "search-v1"):
            self.assert_error("INVALID_ARGUMENT", management_report, {**snapshot, "schema": schema},
                              self.base / "bad.html", config=self.config, store=self.store)

    def test_report_rejects_preview_hash_change_with_same_content_version(self):
        snapshot = management.photo(self.ids[0], store=self.store)
        before = deepcopy(snapshot)
        self.store.put_thumbnail(self.store.photo(self.ids[0]), self.jpeg((210, 220, 230)))
        page = self.report(snapshot)
        self.assertNotIn('src="data:image/jpeg;base64,', page)
        self.assertIn("Preview changed since this snapshot", page)
        self.assertEqual(before, snapshot)

    def test_report_rejects_content_or_profile_changes(self):
        for field in ("content_version", "thumbnail_profile"):
            original = self.store.photo(self.ids[0])
            snapshot = management.photo(self.ids[0], store=self.store)
            self.store.put_photo({**original, field: "changed"})
            self.assertNotIn('src="data:image/jpeg;base64,', self.report(snapshot))
            self.assertEqual(snapshot["items"][0][field], original[field])
            self.store.put_photo(original)
        snapshot = management.photo(self.ids[0], store=self.store)
        self.store.db.execute("UPDATE thumbnails SET profile=? WHERE photo_id=?", ("changed", self.ids[0]))
        self.assertIn("Preview changed since this snapshot", self.report(snapshot))

    def test_report_corrupt_preview_is_per_photo_error(self):
        snapshot = management.photos(store=self.store)
        self.store.db.execute("UPDATE thumbnails SET data=? WHERE photo_id=?", (b"not-a-jpeg", self.ids[0]))
        page = self.report(snapshot)
        self.assertEqual(page.count('src="data:image/jpeg;base64,'), 5)
        self.assertIn("预览不可用", page)
        self.assertIn("content check", page)

    def test_report_validates_jpeg_bytes_size_mime_and_dimensions(self):
        for field, value in (("size_bytes", 1), ("mime_type", "image/png"), ("width", 31)):
            original = self.store.thumbnail(self.ids[0])
            self.store.db.execute(f"UPDATE thumbnails SET {field}=? WHERE photo_id=?", (value, self.ids[0]))
            self.assertNotIn('src="data:image/jpeg;base64,', self.report(management.photo(self.ids[0], store=self.store)))
            self.store.put_thumbnail(self.store.photo(self.ids[0]), original["data"])
        buffer = io.BytesIO()
        Image.new("RGB", (32, 24)).save(buffer, format="PNG")
        data = buffer.getvalue()
        self.store.db.execute("UPDATE thumbnails SET data=?,image_hash=? WHERE photo_id=?",
                              (data, hashlib.sha256(data).hexdigest(), self.ids[0]))
        self.assertNotIn('src="data:image/jpeg;base64,', self.report(management.photo(self.ids[0], store=self.store)))

    def test_report_preserves_missing_preview_and_empty_snapshots(self):
        self.store.db.execute("DELETE FROM thumbnails WHERE photo_id=?", (self.ids[0],))
        snapshot = management.photo(self.ids[0], store=self.store)
        self.assertIsNone(snapshot["items"][0]["input_image_hash"])
        self.assertIn("预览不可用", self.report(snapshot))
        self.assertIn("此快照没有照片结果", self.report(management.metadata_search("no-such-name", store=self.store)))

    def test_original_absolute_available_persists_status_then_needs_no_writer(self):
        original = self.materialize()
        first = self.command("original", self.ids[0])
        self.assertEqual(first["status"], "available")
        self.assertEqual(first["resolution"]["path"], str(original))
        self.assertEqual(first["resolution"]["via"], "absolute")
        self.assertFalse(first["resolution"]["content_verified"])
        self.assertTrue(first["persisted"])
        with patch.object(SQLiteStorage, "open", side_effect=AssertionError("No writer needed")):
            second = self.command("original", self.ids[0])
        self.assertFalse(second["persisted"])

    def test_original_relative_repair_preserves_photo_and_embedding_identity(self):
        new_path = self.materialize()
        photo = self.store.photo(self.ids[0])
        self.store.put_photo({**photo, "original_absolute_path": str(self.base / "old-missing.jpg")})
        self.configure()
        result_id = self.seed(self.ids[0])
        with SQLiteStorage.open(self.config.database_path) as reader:
            result = management.original(self.ids[0], store=reader)
        self.assertTrue(result["path_repaired"])
        self.assertEqual(result["resolution"]["via"], "relative")
        updated = self.store.photo(self.ids[0])
        self.assertEqual(updated["original_absolute_path"], str(new_path))
        for field in ("photo_id", "content_version", "thumbnail_profile", "updated_at"):
            self.assertEqual(updated[field], photo[field])
        self.assertEqual(self.search()["results"][0]["result_id"], result_id)

    def test_original_missing_preserves_saved_preview_and_returns_metadata(self):
        result = self.command("original", self.ids[0])
        self.assertEqual(result["status"], "missing")
        self.assertTrue(result["persisted"])
        self.assertEqual(result["photo"]["metadata"]["width"], 32)
        self.assertEqual(self.store.photo(self.ids[0])["original_status"], "missing")
        self.assertIn("data:image/jpeg", self.report(management.photo(self.ids[0], store=self.store)))

    def test_original_unavailable_is_an_error_not_missing(self):
        record = self.store.photo(self.ids[0])
        resolution = {"status": "unavailable", "path": record["original_absolute_path"], "via": "absolute",
                      "repair_required": False, "content_verified": False, "checked_at": "2026-09-06",
                      "error": {"code": "ORIGINAL_UNAVAILABLE", "message": "permission denied"}}
        with patch.object(source_paths, "resolve_original", return_value=resolution):
            error = self.assert_error("ORIGINAL_UNAVAILABLE", self.command, "original", self.ids[0])
        self.assertEqual(error.details["resolution"]["status"], "unavailable")
        self.assertEqual(self.store.photo(self.ids[0])["original_status"], "unavailable")
        self.assertEqual(error.details["album"], self.store.album())

    def test_original_readonly_repair_failure_exposes_resolution_without_false_success(self):
        original = self.materialize()
        photo = self.store.photo(self.ids[0])
        self.store.put_photo({**photo, "original_absolute_path": str(self.base / "old-missing.jpg")})
        before = self.store.photo(self.ids[0])
        with SQLiteStorage.open(self.config.database_path) as reader:
            with patch.object(SQLiteStorage, "open", side_effect=PhotographyError("STORAGE_READ_ONLY", "readonly")):
                error = self.assert_error("STORAGE_READ_ONLY", management.original, self.ids[0], store=reader)
        self.assertFalse(error.details["persisted"])
        self.assertEqual(error.details["resolution"]["path"], str(original))
        self.assertEqual(error.details["resolution"]["status"], "available")
        self.assertEqual(self.store.photo(self.ids[0]), before)

    def test_relink_checks_hash_before_opening_writer_and_preserves_embedding(self):
        self.configure()
        result_id = self.seed(self.ids[0])
        original = self.store.photo(self.ids[0])
        new_path = self.base / "new-original.jpg"
        new_path.write_bytes(self.previews[self.ids[0]])
        actual_relink = source_paths.relink_original
        actual_open = SQLiteStorage.open
        with patch.object(SQLiteStorage, "open", wraps=actual_open) as open_store:
            def check_first(*args):
                self.assertFalse(self.store.db.in_transaction)
                self.assertEqual(open_store.call_count, 0)
                return actual_relink(*args)

            with patch.object(source_paths, "relink_original", side_effect=check_first):
                result = self.command("relink", self.ids[0], "--path", str(new_path))
        self.assertTrue(result["resolution"]["content_verified"])
        self.assertTrue(result["persisted"])
        updated = self.store.photo(self.ids[0])
        self.assertEqual(updated["original_absolute_path"], str(new_path))
        self.assertEqual(updated["original_relative_path"], "new-original.jpg")
        self.assertEqual(updated["content_version"], original["content_version"])
        self.assertEqual(self.search()["results"][0]["result_id"], result_id)

    def test_relink_rejects_changed_content_and_relative_path_before_write(self):
        new_path = self.base / "different.jpg"
        new_path.write_bytes(self.jpeg((2, 3, 4)))
        before = self.store.photo(self.ids[0])
        with patch.object(SQLiteStorage, "open", side_effect=AssertionError("No writer before hash verification")):
            self.assert_error("ORIGINAL_CONTENT_MISMATCH", self.command, "relink", self.ids[0], "--path", str(new_path))
            self.assert_error("ORIGINAL_PATH_INVALID", self.command, "relink", self.ids[0], "--path", "relative.jpg")
        self.assertEqual(self.store.photo(self.ids[0]), before)

    def test_path_compare_and_swap_conflict_is_not_hidden(self):
        new_path = self.materialize()
        old = self.store.photo(self.ids[0])
        self.store.put_photo({**old, "original_absolute_path": str(self.base / "old-missing.jpg")})
        actual_resolve = source_paths.resolve_original

        def concurrently_change(record, database_path):
            resolution = actual_resolve(record, database_path)
            self.store.put_photo({**record, "original_relative_path": "concurrent.jpg"})
            return resolution

        with patch.object(source_paths, "resolve_original", side_effect=concurrently_change):
            error = self.assert_error("PHOTO_PATH_CHANGED", self.command, "original", self.ids[0])
        self.assertFalse(error.details["persisted"])
        self.assertEqual(error.details["resolution"]["path"], str(new_path))
        self.assertEqual(self.store.photo(self.ids[0])["original_relative_path"], "concurrent.jpg")

    def test_backup_contains_album_previews_vectors_and_never_overwrites(self):
        self.configure()
        self.seed(self.ids[0])
        backup_path = self.base / "backup.sqlite"
        result = self.command("backup", "--output", str(backup_path))
        self.assertEqual(result["album"], self.store.album())
        self.assertEqual(result["model_calls"], 0)
        with SQLiteStorage.open(backup_path) as backup:
            self.assertEqual(backup.album()["id"], self.store.album()["id"])
            self.assertEqual(len(backup.photos()), 6)
            self.assertEqual(backup.thumbnail(self.ids[0])["data"], self.previews[self.ids[0]])
            self.assertEqual(backup.default_embedding_profile(), self.profile_id)
        before = backup_path.read_bytes()
        with self.assertRaises(PhotographyError):
            self.command("backup", "--output", str(backup_path))
        self.assertEqual(backup_path.read_bytes(), before)

    def test_scan_diagnostics_preserve_pagination_and_errors(self):
        self.store.start_scan("scan-test", str(self.source), self.source.name)
        self.store.event("scan-test", "added", self.ids[0], "first.jpg")
        self.store.event("scan-test", "unchanged", self.ids[1], "second.jpg")
        self.store.event("scan-test", "error", self.ids[2], "third.jpg",
                         {"code": "INVALID_IMAGE", "message": "synthetic failure"})
        self.store.finish_scan("scan-test", {"status": "partial", "errors": 1})
        summary = self.command("scan", "scan-test")
        self.assertEqual(summary["result"]["errors"], 1)
        self.assertEqual(summary["album"], self.store.album())
        first = self.command("scan-events", "scan-test", "--limit", "1", "--changes-only")
        second = self.command("scan-events", "scan-test", "--limit", "1", "--changes-only",
                              "--after", str(first["next_cursor"]))
        self.assertEqual(first["items"][0]["kind"], "added")
        self.assertEqual(second["items"][0]["error"]["code"], "INVALID_IMAGE")
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(second["album"], self.store.album())
        self.assert_error("INVALID_ARGUMENT", self.command, "scan-events", "scan-test", "--after", "-1")
        self.assert_error("SCAN_NOT_FOUND", self.command, "scan", "missing")


if __name__ == "__main__":
    unittest.main()
