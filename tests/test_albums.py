from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from legacy_fixtures import seed_observation
from photography_lib import Config, ingest
from photography_lib.cli import main
from photography_lib.config import PhotographyError
from photography_lib.management import photos as management_photos
from photography_lib.management_report import management_report
from photography_lib.sqlite_storage import SQLiteStorage
from photography_lib.thumbnails import stored_preview
from PIL import Image


class AlbumTests(unittest.TestCase):
    def setUp(self):
        task_temp = tempfile.TemporaryDirectory(prefix="photography-albums-")
        self.addCleanup(task_temp.cleanup)
        self.base = Path(task_temp.name).resolve()
        self.photos = self.base / "originals"
        self.photos.mkdir()
        self.config = Config(self.base / "state", thumbnail_size=128)
        for i in range(3):
            Image.new("RGB", (300, 180), (20 * i, 80, 120)).save(self.photos / f"{i}.jpg")
        self.scan = ingest(self.photos, config=self.config, album_name="旅行")
        self.ids = self.scan["successful_photo_ids"]
        self.album_id = self.scan["album"]["album_id"]

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--state-dir", str(self.config.state_dir), *args])
        return code, json.loads(output.getvalue())

    def legacy(self, version=2):
        with SQLiteStorage(self.config.state_dir) as store, store.transaction():
            originals = store.photos_for_library(self.scan["library_id"])
            for photo in originals:
                data = store.thumbnail(photo["photo_id"])["data"]
                relative = "thumbnails/" + photo["photo_id"] + ".jpg"
                path = self.config.state_dir / relative
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(data)
                photo.pop("thumbnail_id")
                photo["thumbnail_path"] = relative
                store.put_photo(photo)
            for table in ("photo_embeddings", "embedding_encoders", "album_photos", "albums", "thumbnails"):
                store.db.execute(f"DROP TABLE IF EXISTS {table}")
            if version == 1:
                store.db.execute("DROP TABLE IF EXISTS analyses")
                store.db.execute("DROP TABLE IF EXISTS analysis_runs")
            store.db.execute(f"PRAGMA user_version={version}")
        return originals

    def test_many_albums_share_one_photo_and_preview(self):
        with SQLiteStorage(self.config.state_dir) as store:
            before = store.photo(self.ids[0])
            thumbnail = store.thumbnail(self.ids[0])
        second = ingest(self.photos, config=self.config, album_name="黑白")
        again = ingest(self.photos, config=self.config, album_name="黑白")
        self.assertEqual((second["unchanged"], second["album_added"], again["album_added"]), (3, 3, 0))
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertEqual(store.db.execute("SELECT count(*) FROM photos").fetchone()[0], 3)
            self.assertEqual(store.db.execute("SELECT count(*) FROM thumbnails").fetchone()[0], 3)
            self.assertEqual(store.db.execute("SELECT count(*) FROM album_photos").fetchone()[0], 6)
            store.change_members(self.album_id, [self.ids[0]], remove=True)
            self.assertEqual(len(store.photos_for_album(self.album_id)), 2)
            self.assertEqual(len(store.photos_for_album(second["album"]["album_id"])), 3)
            self.assertEqual(store.photo(self.ids[0]), before)
            self.assertEqual(store.thumbnail(self.ids[0]), thumbnail)

    def test_album_names_pagination_and_atomic_members(self):
        with SQLiteStorage(self.config.state_dir) as store:
            album = store.create_album(" Portfolio ")
            self.assertEqual(store.create_album("portfolio")["album_id"], album["album_id"])
            with self.assertRaises(PhotographyError):
                store.change_members(album["album_id"], [self.ids[0], "missing-id"])
            self.assertEqual(store.photos_for_album(album["album_id"]), [])
            store.change_members(album["album_id"], self.ids + self.ids)
            page = store.album_photos(album["album_id"], 1)
            tail = store.album_photos(album["album_id"], 100, page["next_cursor"])
            self.assertEqual(len(page["items"]) + len(tail["items"]), 3)
            self.assertTrue(all("data" not in p and "thumbnail_path" not in p for p in tail["items"]))
            self.assertEqual(store.rename_album(album["album_id"], "作品集")["album_id"], album["album_id"])
            with self.assertRaises(PhotographyError):
                store.rename_album(album["album_id"], "旅行")

    def test_no_album_does_not_attach_new_photos_or_create_defaults(self):
        Image.new("RGB", (40, 40), "red").save(self.photos / "new.jpg")
        plain = ingest(self.photos, config=self.config)
        self.assertIsNone(plain["album"])
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertEqual(len(store.albums()), 1)
            self.assertEqual(len(store.photos_for_album(self.album_id)), 3)
        attached = ingest(self.photos, config=self.config, album_name="旅行")
        self.assertEqual((attached["album_added"], attached["unchanged"]), (1, 4))

    def test_bad_and_missing_files_do_not_remove_members(self):
        (self.photos / "0.jpg").write_bytes(b"broken")
        (self.photos / "1.jpg").unlink()
        (self.photos / "new.jpg").write_bytes(b"broken")
        result = ingest(self.photos, config=self.config, album_name="旅行")
        self.assertEqual((result["failed"], result["missing"], result["album_unchanged"]), (2, 1, 1))
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertEqual(len(store.photos_for_album(self.album_id)), 3)

    def test_preview_write_failure_rolls_back_photo_and_membership(self):
        Image.new("RGB", (40, 40), "red").save(self.photos / "new.jpg")
        original_write = SQLiteStorage.put_thumbnail
        def fail(store, photo, data):
            if photo["relative_path"] == "new.jpg":
                raise PhotographyError("TEST_WRITE_FAILURE", "Simulated preview write failure")
            original_write(store, photo, data)
        with patch.object(SQLiteStorage, "put_thumbnail", fail):
            result = ingest(self.photos, config=self.config, album_name="旅行")
        self.assertEqual(result["failed"], 1)
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertEqual(len(store.photos_for_album(self.album_id)), 3)
            self.assertEqual(len(store.photos_for_library(self.scan["library_id"])), 3)

    def test_corrupt_blob_is_repaired_by_rescan(self):
        with SQLiteStorage(self.config.state_dir) as store:
            original = store.thumbnail(self.ids[0])["data"]
            store.db.execute("UPDATE thumbnails SET data=? WHERE photo_id=?", (b"bad", self.ids[0]))
        result = ingest(self.photos, config=self.config)
        self.assertEqual(result["updated"], 1)
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertEqual(stored_preview(store.photo(self.ids[0]), store), original)

    def test_metadata_queries_do_not_read_image_blobs(self):
        with SQLiteStorage(self.config.state_dir) as store:
            def deny_blob(action, table, column, *args):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == "thumbnails" and column == "data" else sqlite3.SQLITE_OK
            store.db.set_authorizer(deny_blob)
            self.assertEqual(len(store.album_photos(self.album_id)["items"]), 3)
            self.assertEqual(management_photos(store=store, album_id=self.album_id)["total"], 3)

    def test_ingest_browse_album_and_report_never_initialize_model(self):
        with patch("photography_lib.siglip_embedding.SiglipEncoder", side_effect=AssertionError("Model initialized")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("Network called")):
            self.assertEqual(self.cli("ingest", str(self.photos), "--album-name", "CLI")[0], 0)
            code, result = self.cli("management", "photos", "--album-id", self.album_id, "--limit", "1")
            self.assertEqual((code, result["total"], len(result["items"])), (0, 3, 1))
            self.assertIsNotNone(result["next_cursor"])
            self.assertEqual(self.cli("management", "photos", "--album-id", self.album_id,
                                     "--html", str(self.base / "report.html"))[0], 0)
            self.assertEqual(self.cli("albums")[0], 0)

    def test_offline_browsing_and_export_preserve_saved_results(self):
        with SQLiteStorage(self.config.state_dir) as store:
            before = store.photo(self.ids[0])
        offline = self.base / "offline"
        self.photos.rename(offline)
        with SQLiteStorage(self.config.state_dir) as store:
            result = management_photos(store=store, album_id=self.album_id)
            management_report(result, self.base / "offline.html", config=self.config, store=store)
            self.assertEqual(result["total"], 3)
            page = (self.base / "offline.html").read_text(encoding="utf-8")
            self.assertEqual(page.count("data:image/jpeg;base64,"), 3)
            store.rename_album(self.album_id, "离线相册")
            self.assertEqual(store.photo(self.ids[0]), before)
        code, output = self.cli("thumbnail", self.ids[0], "--output", str(self.base / "export.jpg"))
        self.assertEqual(code, 0)
        self.assertGreater(Path(output["output"]).stat().st_size, 0)

    def test_v2_migration_preserves_bytes_archived_records_and_backup(self):
        with SQLiteStorage(self.config.state_dir) as store:
            archived = seed_observation(store, self.ids[0])
            run = {"run_id": "archived-run", "status": "completed", "results": []}
            store.db.execute("INSERT INTO analysis_runs VALUES (?,?,?)",
                             (run["run_id"], run["status"], json.dumps(run)))
        originals = self.legacy()
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(store.albums(), [])
            for old in originals:
                expected = {k: v for k, v in old.items() if k != "thumbnail_path"}
                expected["thumbnail_id"] = old["photo_id"]
                self.assertEqual(store.photo(old["photo_id"]), expected)
                self.assertEqual(store.thumbnail(old["photo_id"])["data"], (self.config.state_dir / old["thumbnail_path"]).read_bytes())
            self.assertIsNone(store.db.execute("SELECT name FROM sqlite_master WHERE name='analyses'").fetchone())
        backups = list((self.config.state_dir / "backups").glob("schema-v2-*.db"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(json.loads(db.execute("SELECT data_json FROM analysis_runs").fetchone()[0]), run)
            self.assertEqual(json.loads(db.execute("SELECT data_json FROM analyses").fetchone()[0]), archived)

    def test_v1_legacy_files_upgrade_without_analysis(self):
        self.legacy(version=1)
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertEqual(store.db.execute("SELECT count(*) FROM thumbnails").fetchone()[0], 3)
            self.assertIsNone(store.db.execute("SELECT name FROM sqlite_master WHERE name='analyses'").fetchone())

    def test_missing_legacy_preview_rolls_back_then_retries(self):
        originals = self.legacy()
        path = self.config.state_dir / originals[0]["thumbnail_path"]
        data = path.read_bytes()
        path.unlink()
        with self.assertRaises(PhotographyError) as error:
            SQLiteStorage(self.config.state_dir)
        self.assertEqual(error.exception.code, "THUMBNAIL_MIGRATION_FAILED")
        self.assertEqual(error.exception.details[0]["photo_id"], originals[0]["photo_id"])
        with closing(sqlite3.connect(self.config.state_dir / "photography.db")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='thumbnails'").fetchone())
        path.write_bytes(data)
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertEqual(store.db.execute("SELECT count(*) FROM thumbnails").fetchone()[0], 3)

    def test_legacy_external_path_is_rejected_without_reading_it(self):
        originals = self.legacy()
        with closing(sqlite3.connect(self.config.state_dir / "photography.db")) as db:
            photo = originals[0]
            photo["thumbnail_path"] = photo["original_path"]
            db.execute("UPDATE photos SET data_json=? WHERE photo_id=?", (json.dumps(photo), photo["photo_id"]))
            db.commit()
        with self.assertRaises(PhotographyError) as error:
            SQLiteStorage(self.config.state_dir)
        self.assertEqual(error.exception.code, "THUMBNAIL_MIGRATION_FAILED")
        self.assertIn("outside", error.exception.details[0]["message"])

    def test_thumbnail_export_cannot_overwrite_source_or_database(self):
        original = (self.photos / "0.jpg").read_bytes()
        for path in (self.photos / "0.jpg", self.config.state_dir / "export.jpg"):
            code, result = self.cli("thumbnail", self.ids[0], "--output", str(path))
            self.assertEqual((code, result["error"]["code"]), (2, "INVALID_ARGUMENT"))
        self.assertEqual((self.photos / "0.jpg").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
