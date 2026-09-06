from __future__ import annotations

import argparse
import builtins
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from photography_lib import management, management_cli
from photography_lib.config import Config, PhotographyError
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
        self.config = Config(self.base / "state", thumbnail_size=64)
        self.store = SQLiteStorage(self.config.state_dir)
        self.addCleanup(self.store.close)
        self.source = self.base / "offline-originals"
        self.library = self.store.library(str(self.source), str(self.source))
        self.other_library = self.store.library(str(self.base / "other-originals"), str(self.base / "other-originals"))
        self.profile = {"schema": "synthetic-image-index-profile-v1", "model": "offline-test",
                        "dimensions": 3, "dtype": "float32-le", "normalized": True}
        self.profile_id = self.store.put_index_profile(self.profile)
        self.other_profile = {**self.profile, "model": "another-space"}
        self.other_profile_id = self.store.put_index_profile(self.other_profile)
        names = ("旅行/Cafe\u0301.JPG", "Trips/STRASSE.JPG", "literal/%_[x].jpg",
                 "Trips/中文街道.jpg", "Other/no-analysis.jpg", "Other/broken.jpg")
        self.ids = []
        for i, name in enumerate(names, 1):
            photo_id = f"p{i:02d}"
            self.ids.append(photo_id)
            photo = {
                "photo_id": photo_id,
                "library_id": (self.library if i < 6 else self.other_library)["library_id"],
                "relative_path": name, "filename": name.rsplit("/", 1)[-1],
                "original_path": str(self.source / f"{i}.jpg"), "path_key": str(i),
                "content_version": f"version-{i}", "content_hash": f"content-{i}",
                "thumbnail_profile": self.config.thumbnail_profile,
                "state": "missing", "width": 32, "height": 24,
            }
            self.store.put_photo(photo)
            self.store.put_thumbnail(photo, self.jpeg((i * 20, 30, 40)))
        self.album = self.store.create_album("Café 旅行")
        self.other_album = self.store.create_album("Straße")
        self.empty_album = self.store.create_album("Empty")
        self.store.change_members(self.album["album_id"], self.ids[:3])
        self.store.change_members(self.other_album["album_id"], [self.ids[3]])
        original_import = builtins.__import__

        def guard(name, *args, **kwargs):
            if name.split(".")[0] in ("torch", "transformers", "numpy", "huggingface_hub", "tokenizers"):
                raise AssertionError("No optional inference runtime may be imported in these offline tests.")
            return original_import(name, *args, **kwargs)

        self.import_guard = patch("builtins.__import__", side_effect=guard)
        self.import_guard.start()
        self.addCleanup(self.import_guard.stop)
        for method in ("analysis_records", "cached_analysis", "latest_analysis_failure"):
            self.assertFalse(hasattr(self.store, method))

    @staticmethod
    def jpeg(color):
        buffer = io.BytesIO()
        Image.new("RGB", (32, 24), color).save(buffer, format="JPEG")
        return buffer.getvalue()

    def seed(self, photo_id, vector=(1.0, 0.0, 0.0), profile_id=None):
        photo = self.store.photo(photo_id)
        snapshot = {**photo, "input_image_hash": self.store.thumbnail(photo_id, include_data=False)["image_hash"]}
        blob = pack_vector(vector, 3)
        return self.store._put_index_result(snapshot, profile_id or self.profile_id, blob,
                                            hashlib.sha256(blob).hexdigest(), 3)

    def configure(self):
        self.store.set_default_index_profile(self.profile_id)

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

    def test_browse_without_default_is_explicit_and_never_reads_preview_blob(self):
        with patch.object(self.store, "thumbnail", wraps=self.store.thumbnail) as thumbnail:
            page = management.photos(store=self.store)
            single = management.photo(self.ids[0], store=self.store)
            self.assertTrue(all(call.kwargs.get("include_data") is False for call in thumbnail.call_args_list))
        self.assertEqual(page["schema"], "management-snapshot-v1")
        self.assertEqual(page["index_configuration"], "not_configured")
        self.assertIsNone(page["profile_id"])
        self.assertEqual(page["limit"], 100)
        self.assertEqual(len(page["items"]), 6)
        self.assertEqual(single["items"][0]["index"]["status"], "not_configured")
        self.assertEqual(single["items"][0]["preview_integrity"], "unchecked")
        self.assertEqual(single["items"][0]["original_verification"], "not_checked")
        self.assertFalse(self.source.exists())

    def test_photo_pagination_scopes_and_album_detail(self):
        first = management.photos(store=self.store, limit=2)
        second = management.photos(store=self.store, limit=2, after=first["next_cursor"])
        last = management.photos(store=self.store, limit=2, after=second["next_cursor"])
        self.assertEqual([p["photo_id"] for p in first["items"] + second["items"] + last["items"]], self.ids)
        self.assertIsNone(last["next_cursor"])
        selected = management.photos(store=self.store, library_id=self.other_library["library_id"])
        self.assertEqual([p["photo_id"] for p in selected["items"]], [self.ids[-1]])
        detail = management.album(self.album["album_id"], store=self.store, limit=2)
        self.assertEqual(detail["album"]["name"], "Café 旅行")
        self.assertEqual(detail["total"], 3)
        self.assertEqual(detail["next_cursor"], self.ids[1])
        self.assertEqual(len(management.photos(store=self.store, album_id=self.album["album_id"])["items"]), 3)

    def test_album_pagination_is_id_order_not_name_order(self):
        first = management.albums(store=self.store, limit=1)
        second = management.albums(store=self.store, limit=2, after=first["next_cursor"])
        ids = [item["album_id"] for item in first["items"] + second["items"]]
        self.assertEqual(ids, sorted([self.album["album_id"], self.other_album["album_id"], self.empty_album["album_id"]]))
        self.assertIsNone(second["next_cursor"])
        cover = next(item["cover"] for item in first["items"] + second["items"] if item["album_id"] == self.album["album_id"])
        self.assertEqual(cover["photo_id"], self.ids[0])

    def test_metadata_normalization_casefold_and_literal_substrings(self):
        for query, target, expected in (
            ("CAFE\u0301", "albums", self.album["album_id"]),
            ("STRASSE", "albums", self.other_album["album_id"]),
            ("CAFÉ", "photos", self.ids[0]),
            ("straße", "photos", self.ids[1]),
            ("%_[x]", "photos", self.ids[2]),
            ("中文", "photos", self.ids[3]),
        ):
            with self.subTest(query=query):
                result = management.metadata_search(query, target=target, store=self.store)
                key = "album_id" if target == "albums" else "photo_id"
                self.assertEqual([p[key] for p in result["items"]], [expected])
                self.assertEqual(result["model_calls"], 0)
        for query in (".*", "does-not-exist"):
            result = management.metadata_search(query, target="photos", store=self.store)
            self.assertEqual(result["items"], [])
            self.assertEqual(result["total"], 0)

    def test_metadata_photo_fields_and_cursor_are_not_description_search(self):
        photo = self.store.photo(self.ids[-1])
        self.store.put_photo({**photo, "filename": "IndependentFileName.jpg", "description": "must-not-match"})
        result = management.metadata_search("independentfilename", target="photos", store=self.store)
        self.assertEqual([p["photo_id"] for p in result["items"]], [self.ids[-1]])
        self.assertEqual(management.metadata_search("must-not-match", target="photos", store=self.store)["total"], 0)
        page = management.metadata_search("jpg", target="photos", store=self.store, limit=1)
        next_page = management.metadata_search("jpg", target="photos", store=self.store, limit=1, after=page["next_cursor"])
        self.assertEqual(page["total"], 6)
        self.assertEqual(next_page["items"][0]["photo_id"], self.ids[1])
        self.assertEqual(management.metadata_search("旅行", target="photos", store=self.store,
                                                    album_id=self.other_album["album_id"])["items"], [])

    def test_browse_uses_explicit_profile_without_switching_saved_default(self):
        self.configure()
        self.seed(self.ids[0])
        current = management.photo(self.ids[0], store=self.store)
        other = management.photo(self.ids[0], store=self.store, profile_id=self.other_profile_id)
        self.assertEqual(current["items"][0]["index"]["status"], "ready")
        self.assertEqual(other["items"][0]["index"]["status"], "missing")
        self.assertEqual(other["profile_id"], self.other_profile_id)
        self.assertEqual(self.store.default_index_profile(), self.profile_id)

    def test_parameter_validation(self):
        for limit in (0, -1, 1001, True, 1.5, "2"):
            with self.subTest(limit=limit):
                self.assert_error("INVALID_ARGUMENT", management.photos, store=self.store, limit=limit)
                self.assert_error("INVALID_ARGUMENT", self.search, limit=limit)
        for query in ("", " \t\n"):
            self.assert_error("INVALID_ARGUMENT", management.metadata_search, query, target="photos", store=self.store)
            self.assert_error("INVALID_ARGUMENT", self.search, query)
        self.assert_error("INVALID_ARGUMENT", management.photos, store=self.store, after=3)
        self.assert_error("INVALID_ARGUMENT", management.photos, store=self.store, album_id="x", library_id="y")
        self.assert_error("INVALID_ARGUMENT", management.photos, store=self.store, album_id="")
        self.assert_error("INVALID_ARGUMENT", management.metadata_search, "x", target="wrong", store=self.store)
        self.assert_error("INVALID_ARGUMENT", management.metadata_search, "x", target="albums", store=self.store, album_id="x")
        for after in ("", "p01"):
            self.assert_error("INVALID_ARGUMENT", self.search, after=after)

    def test_unknown_scope_and_photo_errors(self):
        self.assert_error("ALBUM_NOT_FOUND", management.album, "absent", store=self.store)
        self.assert_error("ALBUM_NOT_FOUND", management.photos, album_id="absent", store=self.store)
        self.assert_error("LIBRARY_NOT_FOUND", management.photos, library_id="absent", store=self.store)
        self.assert_error("PHOTO_NOT_FOUND", management.photo, "absent", store=self.store)

    def test_semantic_requires_saved_or_explicit_profile_and_empty_scope_skips_encoder(self):
        encoder = FakeEncoder(self.profile)
        with self.assertRaises(PhotographyError):
            self.search(encoder=encoder)
        self.assertEqual(encoder.queries, [])
        result = self.search(profile_id=self.profile_id, encoder=encoder)
        self.assertEqual(result["coverage"], {"total": 6, "ready": 0, "missing": 6, "stale": 0,
                                             "invalid_input": 0, "invalid_vector": 0})
        self.assertEqual(result["results"], [])
        self.assertEqual(result["model_calls"], 0)
        self.assertIsNone(result["timings"]["query_encoding_seconds"])
        self.assertEqual(encoder.queries, [])
        self.assertIsNone(self.store.default_index_profile())
        self.configure()
        empty = self.search(album_id=self.empty_album["album_id"])
        self.assertEqual(empty["coverage"]["total"], 0)
        self.assertEqual(empty["model_calls"], 0)
        without_runtime = management.semantic_search("query", store=self.store, profile_id=self.profile_id)
        self.assertEqual(without_runtime["results"], [])

    def test_semantic_default_limit_exact_cosine_and_ties(self):
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
        self.assertEqual([p["photo_id"] for p in result["results"]], self.ids[:4])
        self.assertEqual([p["result_id"] for p in result["results"][:2]], [first_id, second_id])
        self.assertEqual([p["score"] for p in result["results"][:2]], [1.0, 1.0])
        self.assertAlmostEqual(result["results"][2]["score"], 0.6, places=6)
        self.assertEqual(result["results"][3]["score"], -1.0)
        self.assertEqual(result["coverage"]["ready"], 4)
        for item in result["results"]:
            self.assertEqual(item["profile_id"], self.profile_id)
            self.assertEqual(item["input_image_hash"], self.store.thumbnail(item["photo_id"], include_data=False)["image_hash"])
            self.assertNotIn("analysis_id", item)
            self.assertNotIn("description", item)
            self.assertNotIn("vector", item)

    def test_cosine_ranking_divides_by_actual_norm_instead_of_using_dot_product(self):
        self.configure()
        self.seed(self.ids[0], (1.0, 0.0, 0.0))
        self.seed(self.ids[1], (1.00001, 0.0, 0.0))
        result = self.search()
        self.assertEqual([p["photo_id"] for p in result["results"]], self.ids[:2])
        self.assertEqual([p["score"] for p in result["results"]], [1.0, 1.0])

    def test_coverage_is_entire_scope_not_top_k_or_other_profiles(self):
        self.configure()
        self.seed(self.ids[0])
        self.seed(self.ids[1])
        self.seed(self.ids[2], profile_id=self.other_profile_id)
        self.seed(self.ids[3])
        changed = {**self.store.photo(self.ids[3]), "content_version": "updated"}
        self.store.put_photo(changed)
        self.store.put_thumbnail(changed, self.jpeg((9, 8, 7)))
        error = {**self.store.photo(self.ids[4]), "state": "error"}
        self.store.put_photo(error)
        damaged_id = self.seed(self.ids[5])
        self.store.db.execute("UPDATE image_index_results SET vector_hash=? WHERE result_id=?", ("damaged", damaged_id))
        result = self.search(limit=1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["coverage"], {"total": 6, "ready": 2, "missing": 1, "stale": 1,
                                             "invalid_input": 1, "invalid_vector": 1})
        self.assertEqual(len(result["coverage_items"]), 6)
        selected = self.search(album_id=self.album["album_id"], limit=1)
        self.assertEqual(selected["coverage"]["total"], 3)
        alternate = self.search(profile_id=self.other_profile_id, encoder=FakeEncoder(self.other_profile))
        self.assertEqual([p["photo_id"] for p in alternate["results"]], [self.ids[2]])
        self.assertEqual(self.store.default_index_profile(), self.profile_id)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_index_results").fetchone()[0], 5)

    def test_query_profile_mismatch_and_invalid_query_vectors_fail_closed(self):
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
        self.store.db.execute("PRAGMA journal_mode=WAL")
        writer = SQLiteStorage(self.config.state_dir)
        self.addCleanup(writer.close)
        original_select = self.store.index_photos

        def change_after_selection(**scope):
            photos = original_select(**scope)
            changed = {**writer.photo(self.ids[0]), "content_version": "concurrent-new-version"}
            with writer.transaction():
                writer.put_photo(changed)
                writer.put_thumbnail(changed, self.jpeg((200, 10, 20)))
            return photos

        with patch.object(self.store, "index_photos", side_effect=change_after_selection):
            result = self.search()
        self.assertEqual(result["coverage"]["ready"], 1)
        self.assertEqual(result["results"][0]["result_id"], result_id)
        self.assertEqual(result["results"][0]["content_version"], "version-1")
        self.assertEqual(self.store.photo(self.ids[0])["content_version"], "concurrent-new-version")

    def test_cli_contract_and_conditional_search_defaults(self):
        self.assertEqual(self.command("albums")["view"], "albums")
        self.assertEqual(self.command("album", self.album["album_id"])["total"], 3)
        self.assertEqual(self.command("photo", self.ids[0])["view"], "photo")
        self.assertEqual(self.command("photos")["limit"], 100)
        metadata = self.command("search", "jpg", "--mode", "metadata", "--target", "photos")
        self.assertEqual(metadata["limit"], 100)
        semantic = self.command("search", "anything", "--mode", "semantic", "--profile-id", self.profile_id)
        self.assertEqual(semantic["limit"], 10)
        for arguments in (
            ("search", "jpg", "--mode", "metadata"),
            ("search", "jpg", "--mode", "semantic", "--target", "albums"),
            ("search", "jpg", "--mode", "semantic", "--after", ""),
        ):
            self.assert_error("INVALID_ARGUMENT", self.command, *arguments)

    def test_cli_has_no_write_operations_or_automatic_mode(self):
        for arguments in (("album-create", "x"), ("search", "query"), ("photos", "--album-id", "a", "--library-id", "b")):
            with self.subTest(arguments=arguments), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                self.parse(*arguments)

    def test_semantic_custom_model_directory_preserves_profile_identity(self):
        self.configure()
        self.seed(self.ids[0])
        directory = self.base / "relocated-model"
        encoder = FakeEncoder(self.profile)
        with patch("photography_lib.siglip_embedding.SiglipEncoder", return_value=encoder) as constructor:
            result = self.command("search", "query", "--mode", "semantic", "--model-dir", str(directory))
        constructor.assert_called_once_with(str(directory), profile=self.profile)
        self.assertEqual(result["profile_id"], self.profile_id)
        self.assertEqual(encoder.queries, ["query"])
        self.assertFalse(directory.exists())
        self.assert_error("INVALID_ARGUMENT", self.command, "search", "query", "--mode", "metadata",
                          "--target", "photos", "--model-dir", str(directory))

    def test_exports_validate_all_targets_before_search_or_encoding(self):
        self.configure()
        self.seed(self.ids[0])
        valid = self.base / "report.json"
        for output in (self.config.state_dir / "report.html", self.source / "report.html", self.base / "bad.txt"):
            with self.subTest(output=output), patch.object(management, "semantic_search",
                    side_effect=AssertionError("Export validation must precede search")):
                self.assert_error("INVALID_ARGUMENT", self.command, "search", "query", "--mode", "semantic",
                                  "--output", str(valid), "--html", str(output))
            self.assertFalse(valid.exists())
        with patch.object(management, "semantic_search", side_effect=AssertionError("Must not search")):
            self.assert_error("INVALID_ARGUMENT", self.command, "search", "query", "--mode", "semantic",
                              "--output", str(self.config.state_dir / "report.json"))

    def test_json_and_html_exports_preserve_read_only_business_state(self):
        before = self.store.db.total_changes
        output = self.base / "view.json"
        report = self.base / "view.html"
        result = self.command("photos", "--output", str(output), "--html", str(report))
        self.assertEqual(result["html_output"], str(report))
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result)
        self.assertIn("data:image/jpeg;base64", report.read_text(encoding="utf-8"))
        self.assertEqual(self.store.db.total_changes, before)

    def test_semantic_and_metadata_do_not_write_state_or_memberships(self):
        self.configure()
        self.seed(self.ids[0])
        before = self.store.db.total_changes
        members = self.store.photos_for_album(self.album["album_id"])
        self.search()
        management.metadata_search("Café", target="albums", store=self.store)
        management.albums(store=self.store)
        self.assertEqual(self.store.db.total_changes, before)
        self.assertEqual(self.store.photos_for_album(self.album["album_id"]), members)

    def test_report_escapes_metadata_query_and_embedded_json_without_widgets(self):
        self.configure()
        self.seed(self.ids[0])
        result = self.search('<script>alert("query")</script>')
        result["results"][0]["relative_path"] = "</pre><img src=x onerror=alert(1)>"
        before = deepcopy(result)
        output = self.base / "safe.html"
        management_report(result, output, config=self.config, store=self.store)
        page = output.read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;/pre&gt;&lt;img", page)
        self.assertNotIn("<script", page)
        self.assertNotIn("<img src=x", page)
        for forbidden in ("<input", "<textarea", "<button", "<form", "search-add", "selected-photo-ids"):
            self.assertNotIn(forbidden, page)
        self.assertIn("data:image/jpeg;base64", page)
        self.assertIn("not a probability", page)
        self.assertEqual(before, result)

    def test_report_does_not_stat_or_read_original_paths(self):
        result = management.photos(store=self.store)
        original_open = Path.open
        original_stat = Path.stat

        def guard_open(path, *args, **kwargs):
            if path.is_relative_to(self.source):
                raise AssertionError("Originals may not be read.")
            return original_open(path, *args, **kwargs)

        def guard_stat(path, *args, **kwargs):
            if path != self.source and path.is_relative_to(self.source):
                raise AssertionError("Originals may not be statted.")
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "open", guard_open), patch.object(Path, "stat", guard_stat):
            output = self.base / "offline.html"
            management_report(result, output, config=self.config, store=self.store)
        self.assertEqual(output.read_text(encoding="utf-8").count('src="data:image/jpeg;base64,'), 6)

    def test_report_rejects_thumbnail_hash_change_even_when_version_and_profile_match(self):
        self.configure()
        self.seed(self.ids[0])
        result = self.search()
        before = deepcopy(result)
        self.store.put_thumbnail(self.store.photo(self.ids[0]), self.jpeg((210, 220, 230)))
        output = self.base / "changed.html"
        management_report(result, output, config=self.config, store=self.store)
        page = output.read_text(encoding="utf-8")
        self.assertNotIn('src="data:image/jpeg;base64,', page)
        self.assertIn("Preview changed since this snapshot", page)
        self.assertEqual(before, result)

    def test_report_rejects_content_or_thumbnail_profile_change(self):
        for field in ("content_version", "thumbnail_profile"):
            original = self.store.photo(self.ids[0])
            snapshot = management.photo(self.ids[0], store=self.store)
            self.store.put_photo({**original, field: "changed"})
            output = self.base / (field + ".html")
            management_report(snapshot, output, config=self.config, store=self.store)
            self.assertNotIn('src="data:image/jpeg;base64,', output.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["items"][0][field], original[field])
            self.store.put_photo(original)

    def test_report_rejects_changed_thumbnail_row_profile(self):
        snapshot = management.photo(self.ids[0], store=self.store)
        self.store.db.execute("UPDATE thumbnails SET profile=? WHERE photo_id=?", ("changed", self.ids[0]))
        output = self.base / "changed-row-profile.html"
        management_report(snapshot, output, config=self.config, store=self.store)
        page = output.read_text(encoding="utf-8")
        self.assertNotIn('src="data:image/jpeg;base64,', page)
        self.assertIn("Preview changed since this snapshot", page)

    def test_report_corrupt_preview_is_per_photo_error_not_replacement(self):
        snapshot = management.photos(store=self.store)
        self.store.db.execute("UPDATE thumbnails SET data=? WHERE photo_id=?", (b"not-a-jpeg", self.ids[0]))
        output = self.base / "broken.html"
        management_report(snapshot, output, config=self.config, store=self.store)
        page = output.read_text(encoding="utf-8")
        self.assertEqual(page.count('src="data:image/jpeg;base64,'), 5)
        self.assertIn("Preview unavailable", page)
        self.assertIn("content check", page)

    def test_report_rejects_non_jpeg_bytes_even_with_updated_valid_hash(self):
        buffer = io.BytesIO()
        Image.new("RGB", (32, 24)).save(buffer, format="PNG")
        data = buffer.getvalue()
        self.store.db.execute("UPDATE thumbnails SET data=?, image_hash=? WHERE photo_id=?",
                              (data, hashlib.sha256(data).hexdigest(), self.ids[0]))
        snapshot = management.photo(self.ids[0], store=self.store)
        output = self.base / "not-jpeg.html"
        management_report(snapshot, output, config=self.config, store=self.store)
        self.assertNotIn('src="data:image/jpeg;base64,', output.read_text(encoding="utf-8"))

    def test_report_validates_size_and_mime_metadata(self):
        for field, value in (("size_bytes", 1), ("mime_type", "image/png"), ("width", 31)):
            original = self.store.thumbnail(self.ids[0])
            self.store.db.execute(f"UPDATE thumbnails SET {field}=? WHERE photo_id=?", (value, self.ids[0]))
            snapshot = management.photo(self.ids[0], store=self.store)
            output = self.base / (field + ".html")
            management_report(snapshot, output, config=self.config, store=self.store)
            self.assertNotIn('src="data:image/jpeg;base64,', output.read_text(encoding="utf-8"))
            self.store.put_thumbnail(self.store.photo(self.ids[0]), original["data"])

    def test_report_rejects_protected_target_before_any_preview_read(self):
        snapshot = management.photo(self.ids[0], store=self.store)
        with patch.object(self.store, "thumbnail", side_effect=AssertionError("No preview read for unsafe output")):
            self.assert_error("INVALID_ARGUMENT", management_report, snapshot, self.source / "report.html",
                              config=self.config, store=self.store)

    def test_missing_thumbnail_metadata_and_album_previews_remain_browsable(self):
        self.store.db.execute("DELETE FROM thumbnails WHERE photo_id=?", (self.ids[0],))
        result = management.photo(self.ids[0], store=self.store)
        self.assertIsNone(result["items"][0]["input_image_hash"])
        self.assertEqual(result["items"][0]["preview_error"]["code"], "INVALID_PREVIEW")
        for snapshot in (result, management.albums(store=self.store),
                         management.metadata_search("Café", target="albums", store=self.store)):
            output = self.base / (uuid4().hex + ".html")
            management_report(snapshot, output, config=self.config, store=self.store)
            self.assertIn("Preview unavailable", output.read_text(encoding="utf-8"))

    def test_report_preserves_empty_snapshot_and_rejects_legacy_schema(self):
        snapshot = management.metadata_search("no-such-name", target="albums", store=self.store)
        output = self.base / "empty.html"
        management_report(snapshot, output, config=self.config, store=self.store)
        self.assertIn("No results", output.read_text(encoding="utf-8"))
        self.assert_error("INVALID_ARGUMENT", management_report, {"results": []}, output,
                          config=self.config, store=self.store)


if __name__ == "__main__":
    unittest.main()
