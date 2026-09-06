from __future__ import annotations

import builtins
import hashlib
import io
import json
import sqlite3
import struct
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_analyze import FakeProvider
from PIL import Image
from photography_lib import Config, ingest, analyze
from photography_lib.cli import main, parser
from photography_lib.config import PhotographyError
from photography_lib.search import add_search_selection
from photography_lib.search_report import search_report
from photography_lib.sqlite_storage import SQLiteStorage, SCHEMA, now


class SavedSearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="photography-saved-search-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "originals"
        self.source.mkdir()
        for i in range(3):
            Image.new("RGB", (100, 80), (30 * i, 40, 50)).save(self.source / f"{i}.jpg")
        self.config = Config(self.base / "state", thumbnail_size=64)
        scan = ingest(self.source, config=self.config, album_name="Originals")
        self.library_id = scan["library_id"]
        self.store = SQLiteStorage(self.config.state_dir)
        self.addCleanup(self.store.close)
        photos = sorted(self.store.photos_for_library(self.library_id), key=lambda p: p["relative_path"])
        self.ids = [p["photo_id"] for p in photos]
        analyze(self.ids[:2], config=self.config, provider=FakeProvider(), storage=self.store)
        stamp = now()
        self.store.db.execute("INSERT INTO embedding_encoders VALUES (?,?,?)",
            ("archived-fixture", '{"model":"synthetic-archive-fixture","dimensions":384}', stamp))
        vector = struct.pack("<384f", 1., *([0.] * 383))
        results = []
        for photo in photos[:2]:
            observation = self.store.analysis_records(photo["photo_id"])[0]
            self.store.db.execute("INSERT INTO photo_embeddings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (photo["photo_id"], "archived-fixture", observation["analysis_id"], photo["content_version"],
                 "synthetic-text-hash", "archived-fixture", 384, "float32-le", 1, vector,
                 hashlib.sha256(vector).hexdigest(), 10, 0, stamp))
            results.append({"photo_id": photo["photo_id"], "relative_path": photo["relative_path"],
                "score": .5, "description": "Synthetic saved search result.",
                "analysis_id": observation["analysis_id"], "content_version": photo["content_version"],
                "thumbnail_id": photo["photo_id"]})
        self.snapshot = {"search_id": "saved-fixture", "query": "Synthetic query",
            "encoder_id": "archived-fixture", "results": results,
            "coverage": {"ready": 2, "missing": 0, "stale": 0, "needs_analysis": 1, "invalid": 0, "total": 3}}
        self.snapshot_path = self.base / "search.json"
        self.snapshot_path.write_text(json.dumps(self.snapshot), encoding="utf-8")
        for target in ("photography_lib.vision.OpenAIResponsesProvider.analyze",
                       "photography_lib.codex_vision.CodexCLIProvider.analyze", "urllib.request.urlopen"):
            guard = patch(target, side_effect=AssertionError("No model or network calls allowed"))
            guard.start()
            self.addCleanup(guard.stop)

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--state-dir", str(self.config.state_dir), *args])
        return code, json.loads(output.getvalue())

    def rows(self, table):
        return [tuple(r) for r in self.store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def test_selection_uses_frozen_ids_and_preserves_archived_vectors(self):
        before = self.rows("photo_embeddings")
        photo = self.store.photo(self.ids[0])
        self.store.put_photo({**photo, "content_version": "changed-since-snapshot"})
        first = add_search_selection(self.snapshot, [self.ids[0]], store=self.store, album_name="Selected")
        again = add_search_selection(self.snapshot, [self.ids[0]], store=self.store, album_name="Selected")
        self.assertEqual((first["added"], again["added"]), (1, 0))
        self.assertEqual(first["search_id"], self.snapshot["search_id"])
        self.assertEqual((first["visual_model_calls"], first["local_model_calls"]), (0, 0))
        self.assertEqual(before, self.rows("photo_embeddings"))
        self.assertEqual([p["photo_id"] for p in self.store.photos_for_album(first["album_id"])], [self.ids[0]])

    def test_nonresult_selection_rejected_atomically(self):
        before = self.store.albums()
        for ids in ([], [self.ids[2]], [self.ids[0], self.ids[2]]):
            with self.subTest(ids=ids), self.assertRaises(PhotographyError):
                add_search_selection(self.snapshot, ids, store=self.store, album_name="Must not exist")
        self.assertEqual(before, self.store.albums())

    def test_missing_photo_in_snapshot_rolls_back_album_and_members(self):
        self.snapshot["results"].append({"photo_id": "missing-photo"})
        before = self.store.albums()
        with self.assertRaises(PhotographyError) as error:
            add_search_selection(self.snapshot, [self.ids[0], "missing-photo"],
                                 store=self.store, album_name="Must not exist")
        self.assertEqual(error.exception.code, "PHOTO_NOT_FOUND")
        self.assertEqual(before, self.store.albums())

    def test_cli_selection_accepts_ids_or_exported_file(self):
        code, added = self.cli("search-add", str(self.snapshot_path), self.ids[0], "--album-name", "Selected")
        self.assertEqual((code, added["added"], added["local_model_calls"]), (0, 1, 0))
        selection = self.base / "selection.json"
        selection.write_text(json.dumps([self.ids[0], self.ids[1]]), encoding="utf-8")
        code, added = self.cli("search-add", str(self.snapshot_path), "--album-name", "Selected",
                               "--ids-file", str(selection))
        self.assertEqual((code, added["added"], added["unchanged"]), (0, 1, 1))

    def test_cli_invalid_files_and_mixed_selection_are_structured_errors(self):
        invalid = self.base / "invalid.json"
        for text in ('not JSON', '{}', '[""]', '[1]'):
            invalid.write_text(text, encoding="utf-8")
            code, result = self.cli("search-add", str(self.snapshot_path), "--album-name", "Selected",
                                    "--ids-file", str(invalid))
            self.assertEqual((code, result["error"]["code"]), (2, "INVALID_ARGUMENT"))
        code, result = self.cli("search-add", str(self.snapshot_path), self.ids[0],
                                "--album-name", "Selected", "--ids-file", str(invalid))
        self.assertEqual((code, result["error"]["code"]), (2, "INVALID_ARGUMENT"))
        for path in (invalid, self.base / "missing.json"):
            code, result = self.cli("search-add", str(path), self.ids[0], "--album-name", "Selected")
            self.assertEqual((code, result["error"]["code"]), (2, "INVALID_ARGUMENT"))

    def test_retired_commands_are_rejected_without_creating_state(self):
        state = self.base / "unused-state"
        for command in ("embedding-setup", "embed", "embedding-status", "search", "index"):
            with self.subTest(command=command), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["--state-dir", str(state), command])
            self.assertEqual(error.exception.code, 2)
        self.assertFalse(state.exists())
        self.assertIn("not yet available", " ".join(parser().format_help().split()))

    def test_browsing_and_selection_need_no_originals_or_embedding_runtime(self):
        for photo in self.source.iterdir():
            photo.unlink()
        original_import = builtins.__import__

        def guard(name, *args, **kwargs):
            if name.split(".")[0] in ("onnxruntime", "tokenizers", "numpy"):
                raise AssertionError("No text encoding runtime")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guard):
            self.assertEqual(self.cli("analysis-status", "--library-id", self.library_id)[0], 0)
            self.assertEqual(self.cli("search-add", str(self.snapshot_path), self.ids[0],
                                      "--album-name", "Offline")[0], 0)
            output = self.base / "offline.html"
            search_report(self.snapshot, output, config=self.config, store=self.store)
        self.assertIn("data:image/jpeg;base64", output.read_text(encoding="utf-8"))

    def test_report_escapes_saved_content_and_preserves_ranking(self):
        self.snapshot["query"] = '<img src=x onerror=alert(1)>'
        self.snapshot["results"][0]["description"] = '<script>alert("x")</script>'
        self.snapshot["results"].reverse()
        output = self.base / "search.html"
        search_report(self.snapshot, output, config=self.config, store=self.store)
        page = output.read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("data:image/jpeg;base64", page)
        self.assertLess(page.index(self.ids[1]), page.index(self.ids[0]))

    def test_empty_snapshot_report_still_exports(self):
        self.snapshot["results"] = []
        output = self.base / "empty.html"
        self.assertEqual(search_report(self.snapshot, output, config=self.config, store=self.store), str(output))
        self.assertTrue(output.is_file())

    def test_v5_reopen_preserves_all_rows_and_archive_bytes(self):
        tables = ("libraries", "photos", "thumbnails", "analyses", "analysis_runs", "albums",
                  "album_photos", "embedding_encoders", "photo_embeddings")
        before = {table: self.rows(table) for table in tables}
        self.store.close()
        self.store = SQLiteStorage(self.config.state_dir)
        self.addCleanup(self.store.close)
        self.assertEqual(before, {table: self.rows(table) for table in tables})
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v4_upgrade_preserves_archived_vectors_and_backup(self):
        before = self.rows("photo_embeddings")
        self.store.db.execute("PRAGMA user_version=4")
        self.store.close()
        self.store = SQLiteStorage(self.config.state_dir)
        self.addCleanup(self.store.close)
        self.assertEqual(before, self.rows("photo_embeddings"))
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 5)
        backup = next((self.config.state_dir / "backups").glob("schema-v4-*.db"))
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(before, db.execute("SELECT * FROM photo_embeddings ORDER BY rowid").fetchall())

    def test_v3_migration_preserves_data_and_consistent_backup(self):
        self.store.db.execute("DROP TABLE photo_embeddings")
        self.store.db.execute("DROP TABLE embedding_encoders")
        self.store.db.execute("PRAGMA user_version=3")
        before = {table: self.rows(table) for table in ("thumbnails", "analyses")}
        self.store.close()
        self.store = SQLiteStorage(self.config.state_dir)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertEqual(before, {table: self.rows(table) for table in before})
        backup = next((self.config.state_dir / "backups").glob("schema-v3-*.db"))
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_migration_failure_rolls_back(self):
        self.store.db.execute("DROP TABLE photo_embeddings")
        self.store.db.execute("DROP TABLE embedding_encoders")
        self.store.db.execute("PRAGMA user_version=3")
        self.store.close()
        with patch("photography_lib.sqlite_storage.SCHEMA", SCHEMA + ("INVALID SQL",)), self.assertRaises(PhotographyError):
            SQLiteStorage(self.config.state_dir)
        with closing(sqlite3.connect(self.config.state_dir / "photography.db")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='photo_embeddings'").fetchone())


if __name__ == "__main__":
    unittest.main()
