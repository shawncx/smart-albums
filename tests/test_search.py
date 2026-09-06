from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import sqlite3
import struct
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_analyze import FakeProvider
from PIL import Image
from photography_lib import Config, ingest, analyze
from photography_lib.analysis_schema import fingerprint
from photography_lib.cli import main
from photography_lib.config import PhotographyError
from photography_lib.embedding_model import Encoding, LocalEncoder, pack_vector, unpack_vector, prepare_model
from photography_lib.embedding_text import RECIPE_VERSION, retrieval_text, text_hash
from photography_lib.embeddings import embed, embedding_status
from photography_lib.search import search, add_search_selection
from photography_lib.search_report import search_report
from photography_lib.sqlite_storage import SQLiteStorage, SCHEMA, now


class FakeEncoder:
    """Deterministic geometric fixture, never a production or live-library model."""
    def __init__(self, version="one", callback=None):
        self.version, self.callback, self.calls = version, callback, 0
        self.load_seconds = 0

    def profile(self):
        return {"model": "synthetic-test-only", "version": self.version, "dimensions": 3,
                "recipe_version": RECIPE_VERSION, "dtype": "float32-le", "normalized": True}

    def encode(self, text, *, query=False):
        self.calls += 1
        if self.callback:
            self.callback(self.calls)
        v = [1., 0., 0.] if query or "fixture-0" in text else [.6, .8, 0.] if "fixture-1" in text else [-1., 0., 0.]
        return Encoding(v, 10, False, .001)


class SearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="photography-search-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "originals"
        self.source.mkdir()
        for i in range(4):
            Image.new("RGB", (100, 80), (30 * i, 40, 50)).save(self.source / f"{i}.jpg")
        self.config = Config(self.base / "state", thumbnail_size=64)
        scan = ingest(self.source, config=self.config, album_name="测试")
        self.album_id, self.library_id = scan["album"]["album_id"], scan["library_id"]
        self.store = SQLiteStorage(self.config.state_dir)
        self.addCleanup(self.store.close)
        self.ids = [p["photo_id"] for p in sorted(self.store.photos_for_library(self.library_id), key=lambda p: p["relative_path"])]
        analyze(self.ids[:3], config=self.config, provider=FakeProvider(), storage=self.store)
        for i, pid in enumerate(self.ids[:3]):
            record = self.store.analysis_records(pid)[0]
            record["data"]["visual_description"] = f"fixture-{i}，明确标记的测试描述。"
            self.store.db.execute("UPDATE analyses SET data_json=? WHERE analysis_id=?", (json.dumps(record), record["analysis_id"]))
        for target in ("photography_lib.vision.OpenAIResponsesProvider.analyze", "photography_lib.codex_vision.CodexCLIProvider.analyze", "urllib.request.urlopen"):
            guard = patch(target, side_effect=AssertionError("No remote/model network calls allowed"))
            guard.start()
            self.addCleanup(guard.stop)
        self.encoder = FakeEncoder()

    def build(self, **kwargs):
        return embed(self.ids, store=self.store, encoder=kwargs.pop("encoder", self.encoder), **kwargs)

    def status(self):
        return embedding_status(self.store.search_photos(), self.store, self.encoder.profile())

    def new_analysis(self, pid, description):
        record = copy.deepcopy(self.store.analysis_records(pid)[0])
        record.update(analysis_id=record["analysis_id"] + "_new", created_at=now())
        record["data"]["visual_description"] = description
        self.store.put_analysis(record)

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--state-dir", str(self.config.state_dir), *args])
        return code, json.loads(output.getvalue())

    def test_incremental_counts_shared_album_and_no_vision(self):
        first = self.build()
        self.assertEqual((first["generated"], first["skipped"], first["visual_model_calls"]), (3, 1, 0))
        album = self.store.create_album("第二相册")
        self.store.change_members(album["album_id"], self.ids)
        again = self.build()
        self.assertEqual((again["cached"], again["generated"], self.encoder.calls), (3, 0, 3))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM photo_embeddings").fetchone()[0], 3)

    def test_status_dry_run_and_missing_model_do_not_load(self):
        model = LocalEncoder(self.base / "missing-model")
        result = embed(self.ids, store=self.store, encoder=model, dry_run=True)
        self.assertEqual((result["pending"], result["skipped"], model.calls), (3, 1, 0))
        result = search("空索引", store=self.store, encoder=model)
        self.assertEqual((result["results"], result["local_model_calls"]), ([], 0))
        self.assertEqual(self.cli("embedding-status")[1]["counts"]["missing"], 3)

    def test_missing_model_returns_blocked_and_unprocessed(self):
        result = self.build(encoder=LocalEncoder(self.base / "missing"))
        self.assertEqual((result["status"], result["unprocessed"], result["generated"]), ("blocked", 3, 0))

    def test_cosine_ranking_top_k_and_negative_score(self):
        self.build()
        result = search("query", store=self.store, encoder=self.encoder, limit=3)
        self.assertEqual([r["photo_id"] for r in result["results"]], self.ids[:3])
        for row, expected in zip(result["results"], (1., .6, -1.)):
            self.assertAlmostEqual(row["score"], expected, places=6)
        self.assertEqual(search("query", store=self.store, encoder=self.encoder, limit=1)["results"][0]["photo_id"], self.ids[0])
        self.assertEqual(result["coverage"], {"ready": 3, "missing": 0, "stale": 0, "needs_analysis": 1, "invalid": 0, "total": 4})

    def test_ties_are_stable_by_id(self):
        self.new_analysis(self.ids[1], "fixture-0 duplicate description")
        self.build()
        result = search("query", store=self.store, encoder=self.encoder)
        self.assertEqual([r["photo_id"] for r in result["results"][:2]], sorted(self.ids[:2]))

    def test_album_library_and_empty_scope(self):
        self.build()
        album = self.store.create_album("单张")
        self.store.change_members(album["album_id"], [self.ids[1]])
        result = search("query", store=self.store, encoder=self.encoder, album_id=album["album_id"])
        self.assertEqual([r["photo_id"] for r in result["results"]], [self.ids[1]])
        self.assertEqual(search("query", store=self.store, encoder=self.encoder, library_id=self.library_id)["coverage"]["total"], 4)
        empty = self.store.create_album("空")
        calls = self.encoder.calls
        self.assertEqual(search("query", store=self.store, encoder=self.encoder, album_id=empty["album_id"])["results"], [])
        self.assertEqual(self.encoder.calls, calls)
        with self.assertRaises(PhotographyError):
            search("query", store=self.store, encoder=self.encoder, album_id=self.album_id, library_id=self.library_id)

    def test_encoder_spaces_never_mix_even_same_dimension(self):
        self.build()
        other = FakeEncoder("two")
        self.assertEqual(search("q", store=self.store, encoder=other)["results"], [])
        self.assertEqual(other.calls, 0)
        self.build(encoder=other)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM photo_embeddings").fetchone()[0], 6)

    def test_new_analysis_invalidates_then_rebuilds_one(self):
        self.build()
        self.new_analysis(self.ids[0], "fixture-0 with updated observation")
        self.assertEqual(self.status()["counts"]["stale"], 1)
        self.assertEqual(search("q", store=self.store, encoder=self.encoder)["coverage"]["ready"], 2)
        result = self.build()
        self.assertEqual((result["generated"], result["cached"]), (1, 2))

    def test_text_modified_in_same_record_invalidates_hash(self):
        self.build()
        row = self.store.analysis_records(self.ids[0])[0]
        row["data"]["tags"] = ["new tag"]
        self.store.db.execute("UPDATE analyses SET data_json=? WHERE analysis_id=?", (json.dumps(row), row["analysis_id"]))
        self.assertEqual(self.status()["counts"]["stale"], 1)

    def test_updated_photo_excludes_historical_description(self):
        self.build()
        photo = self.store.photo(self.ids[0])
        photo["content_version"] = "different-indexed-version"
        self.store.put_photo(photo)
        self.assertEqual(self.status()["counts"]["needs_analysis"], 2)
        self.assertEqual(self.build()["skipped"], 2)

    def test_offline_originals_and_no_thumbnail_blob_reads(self):
        self.build()
        for p in self.source.iterdir():
            p.unlink()
        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table == "thumbnails" and column == "data":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.store.db.set_authorizer(authorize)
        with patch("photography_lib.status.source_status", side_effect=AssertionError("No source stat")):
            self.assertEqual(search("q", store=self.store, encoder=self.encoder)["coverage"]["ready"], 3)
            self.assertEqual(self.build()["cached"], 3)

    def test_corrupt_blob_is_excluded_and_repairable(self):
        self.build()
        self.store.db.execute("UPDATE photo_embeddings SET vector=? WHERE photo_id=?", (b"bad", self.ids[0]))
        self.assertEqual(self.status()["counts"]["invalid"], 1)
        self.assertEqual(search("q", store=self.store, encoder=self.encoder)["coverage"]["ready"], 2)
        self.assertEqual(self.build()["generated"], 1)

    def test_nonfinite_and_unnormalized_vectors_rejected(self):
        for values in ([math.nan, 0, 0], [math.inf, 0, 0], [0, 0, 0], [2, 0, 0], [1, 0]):
            with self.subTest(values=values), self.assertRaises(PhotographyError):
                pack_vector(values, 3)
        with self.assertRaises(PhotographyError):
            unpack_vector(b"bad", 3)

    def test_version_change_during_encoding_does_not_replace_old_vector(self):
        self.build()
        old = self.store.embedding(self.ids[0], fingerprint(self.encoder.profile()))
        def change(_):
            self.new_analysis(self.ids[0], "changed mid-encode")
        result = embed([self.ids[0]], store=self.store, encoder=FakeEncoder(callback=change), force=True)
        self.assertEqual(result["results"][0]["error"]["code"], "DESCRIPTION_CHANGED")
        self.assertEqual(self.store.embedding(self.ids[0], fingerprint(self.encoder.profile())), old)
        self.assertEqual(self.status()["counts"]["stale"], 1)

    def test_failure_isolated_and_resume_reuses_finished_rows(self):
        def fail(call):
            if call == 2:
                raise PhotographyError("EMBEDDING_FAILED", "Test failure")
        result = self.build(encoder=FakeEncoder(callback=fail))
        self.assertEqual((result["generated"], result["failed"], result["skipped"]), (2, 1, 1))
        resumed = self.build()
        self.assertEqual((resumed["generated"], resumed["cached"]), (1, 2))

    def test_interrupt_preserves_completed_rows(self):
        def interrupt(call):
            if call == 2:
                raise KeyboardInterrupt()
        result = self.build(encoder=FakeEncoder(callback=interrupt))
        self.assertTrue(result["interrupted"])
        self.assertEqual((result["generated"], result["unprocessed"]), (1, 3))
        self.assertEqual(self.build()["cached"], 1)

    def test_search_has_no_membership_mutations_and_selection_is_frozen(self):
        self.build()
        before = list(self.store.db.execute("SELECT * FROM album_photos"))
        result = search("q", store=self.store, encoder=self.encoder)
        self.assertEqual(list(self.store.db.execute("SELECT * FROM album_photos")), before)
        self.new_analysis(self.ids[0], "different ranking later")
        with patch("photography_lib.search.search", side_effect=AssertionError("Never search again")):
            first = add_search_selection(result, [self.ids[0]], store=self.store, album_name="选片")
            again = add_search_selection(result, [self.ids[0]], store=self.store, album_name="选片")
        self.assertEqual((first["added"], again["added"]), (1, 0))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM photo_embeddings").fetchone()[0], 3)

    def test_nonresult_selection_rejected_atomically(self):
        self.build()
        result = search("q", store=self.store, encoder=self.encoder, limit=1)
        before = self.store.albums()
        with self.assertRaises(PhotographyError):
            add_search_selection(result, [self.ids[3]], store=self.store, album_name="应不存在")
        self.assertEqual(before, self.store.albums())

    def test_report_escapes_query_and_description(self):
        self.new_analysis(self.ids[0], 'fixture-0 <script>alert("x")</script>')
        self.build()
        result = search('<img src=x onerror=alert(1)>', store=self.store, encoder=self.encoder)
        output = self.base / "search.html"
        search_report(result, output, config=self.config, store=self.store)
        page = output.read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn('<img src=x', page)
        self.assertIn("data:image/jpeg;base64", page)
        self.assertIn(self.ids[0], page)

    def test_cli_empty_search_html_and_selection_validation(self):
        code, result = self.cli("search", "尚未建索引", "--html", str(self.base / "empty.html"))
        self.assertEqual((code, result["local_model_calls"]), (0, 0))
        self.assertTrue((self.base / "empty.json").is_file())
        self.assertEqual(self.cli("embed")[0], 2)
        self.assertEqual(self.cli("search", " ")[0], 2)
        self.assertEqual(self.cli("search", "q", "--limit", "0")[0], 2)
        self.assertEqual(self.cli("search", "q", "--encoder-id", "unknown")[0], 2)

    def test_cli_search_add_uses_saved_ids(self):
        self.build()
        result = search("q", store=self.store, encoder=self.encoder, limit=1)
        path = self.base / "ranked.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        code, added = self.cli("search-add", str(path), self.ids[0], "--album-name", "CLI选择")
        self.assertEqual((code, added["added"], added["local_model_calls"]), (0, 1, 0))
        selection = self.base / "selection.json"
        selection.write_text(json.dumps([self.ids[0]]), encoding="utf-8")
        self.assertEqual(self.cli("search-add", str(path), "--album-name", "CLI选择", "--ids-file", str(selection))[1]["added"], 0)

    def test_cli_invalid_ids_file_with_limit_is_a_structured_error(self):
        path = self.base / "invalid.json"
        path.write_text('{"photo_id":"not-an-array"}', encoding="utf-8")
        code, result = self.cli("embed", "--ids-file", str(path), "--limit", "1")
        self.assertEqual((code, result["error"]["code"]), (2, "INVALID_ARGUMENT"))

    def test_optional_runtime_not_imported_for_status_or_empty_search(self):
        import builtins
        original_import = builtins.__import__
        def guard(name, *args, **kwargs):
            if name.split(".")[0] in ("onnxruntime", "tokenizers", "numpy"):
                raise AssertionError("Optional runtime must remain lazy")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=guard):
            self.assertEqual(self.cli("embedding-status")[0], 0)
            self.assertEqual(self.cli("search", "q")[0], 0)

    def test_recipe_stable_and_excludes_technical_boilerplate(self):
        data = self.store.analysis_records(self.ids[0])[0]["data"]
        text = retrieval_text(data)
        other = copy.deepcopy(data)
        other["technical_observations"]["limitations"] = ["different boilerplate"]
        self.assertEqual(text_hash(text), text_hash(retrieval_text(other)))
        self.assertNotIn("limitations", text)
        self.assertLess(text.index("描述"), text.index("主体"))

    def test_v3_migration_preserves_data_and_consistent_backup(self):
        self.store.db.execute("DROP TABLE photo_embeddings")
        self.store.db.execute("DROP TABLE embedding_encoders")
        self.store.db.execute("PRAGMA user_version=3")
        before = [tuple(r) for r in self.store.db.execute("SELECT * FROM thumbnails ORDER BY photo_id")]
        analyses = [tuple(r) for r in self.store.db.execute("SELECT * FROM analyses ORDER BY analysis_id")]
        self.store.close()
        self.store = SQLiteStorage(self.config.state_dir)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(before, [tuple(r) for r in self.store.db.execute("SELECT * FROM thumbnails ORDER BY photo_id")])
        self.assertEqual(analyses, [tuple(r) for r in self.store.db.execute("SELECT * FROM analyses ORDER BY analysis_id")])
        backup = next((self.config.state_dir / "backups").glob("schema-v3-*.db"))
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_v4_migration_failure_rolls_back(self):
        self.store.db.execute("DROP TABLE photo_embeddings")
        self.store.db.execute("DROP TABLE embedding_encoders")
        self.store.db.execute("PRAGMA user_version=3")
        self.store.close()
        with patch("photography_lib.sqlite_storage.SCHEMA", SCHEMA + ("INVALID SQL",)), self.assertRaises(PhotographyError):
            SQLiteStorage(self.config.state_dir)
        with closing(sqlite3.connect(self.config.state_dir / "photography.db")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='photo_embeddings'").fetchone())


class ModelSetupTests(unittest.TestCase):
    def test_download_checksum_failure_preserves_existing_and_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "model.bin"
            target.write_bytes(b"old")
            wanted = b"good"
            files = {"model.bin": (len(wanted), hashlib.sha256(wanted).hexdigest())}
            with patch("photography_lib.embedding_model.FILES", files), patch("urllib.request.urlopen", return_value=io.BytesIO(b"bad!")), self.assertRaises(PhotographyError):
                prepare_model(root)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(list(root.glob("*.part")), [])
            with patch("photography_lib.embedding_model.FILES", files), patch("urllib.request.urlopen", return_value=io.BytesIO(wanted)):
                self.assertEqual(prepare_model(root)["downloaded_files"], 1)
            with patch("photography_lib.embedding_model.FILES", files), patch("urllib.request.urlopen", side_effect=AssertionError("No download")):
                self.assertEqual(prepare_model(root)["reused_files"], 1)


if __name__ == "__main__":
    unittest.main()
