"""Portable album lifecycle tests using disposable, project-local synthetic files."""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import sqlite3
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from PIL import Image
from photography_lib.config import Config, PhotographyError
from photography_lib.sqlite_storage import (
    APPLICATION_ID, PHOTO_OPTIONAL_FIELDS, REQUIRED_COLUMNS, SCHEMA_VERSION, SQLiteStorage,
)
from photography_lib.thumbnails import stored_preview
from photography_lib import sqlite_storage as storage_module


class AlbumFileTests(unittest.TestCase):
    def setUp(self):
        self.base = PROJECT / (".album-file-tests-" + uuid4().hex)
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base)
        self.path = self.base / "旅行 #1.sqlite"

    def photo(self, photo_id="photo_a", **changes):
        result = {
            "photo_id": photo_id,
            "original_absolute_path": str(self.base / "nonexistent-originals" / "猫.jpg"),
            "original_relative_path": "../nonexistent-originals/猫.jpg",
            "content_version": hashlib.sha256(b"synthetic content").hexdigest(),
            "thumbnail_profile": "preview-v1-srgb-128-q85",
            "size_bytes": 123, "mtime_ns": 1700000000000000001,
            "metadata": {"format": "JPEG", "width": 32, "height": 16, "exif": {"artist": "测试"}},
            "ingest_state": "available", "original_status": "not_checked",
            "last_ingest_error": None, "last_path_error": None, "last_original_check": None,
            "created_at": "2026-09-06T00:00:00+00:00", "updated_at": "2026-09-06T00:00:00+00:00",
            "path_updated_at": None,
        }
        result.update(changes)
        return result

    @staticmethod
    def preview():
        output = io.BytesIO()
        Image.new("RGB", (32, 16), "navy").save(output, format="JPEG")
        return output.getvalue()

    def snapshot(self):
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.base.iterdir() if path.is_file()}

    def test_config_requires_explicit_absolute_database_selection(self):
        for path in (None, "", Path("relative.sqlite"), self.base / "album.txt", self.base):
            with self.subTest(path=path), self.assertRaises(PhotographyError):
                Config(path)
        with patch.dict(os.environ, {
            "PHOTOGRAPHY_STATE_DIR": str(self.base),
            "SMART_ALBUMS_DATABASE": str(self.path),
        }):
            with self.assertRaises(PhotographyError) as failure:
                Config.from_env()
        self.assertEqual(failure.exception.code, "DATABASE_REQUIRED")
        self.assertEqual(list(self.base.iterdir()), [])

    def test_config_canonicalizes_without_creating_files_or_caches(self):
        cache = self.base / "machine-models"
        for suffix in (".sqlite", ".sqlite3", ".db", ".DB"):
            with self.subTest(suffix=suffix):
                config = Config(self.base / "absent" / ".." / ("album" + suffix), cache)
                self.assertEqual(config.database_path, self.base / ("album" + suffix))
                self.assertEqual(config.model_cache_root, cache)
                self.assertFalse(hasattr(config, "state_dir"))
        self.assertEqual(list(self.base.iterdir()), [])

    def test_config_cache_environment_and_explicit_override(self):
        with patch.dict(os.environ, {"SMART_ALBUMS_MODEL_CACHE_DIR": str(self.base / "env-cache")}):
            configured = Config.from_env(self.path, thumbnail_size=128, thumbnail_quality=90)
            self.assertEqual(configured.model_cache_root, self.base / "env-cache")
            self.assertEqual(configured.thumbnail_profile, "preview-v1-srgb-128-q90")
            override = Config.from_env(self.path, model_cache_root=self.base / "override")
            self.assertEqual(override.model_cache_root, self.base / "override")
        self.assertEqual(list(self.base.iterdir()), [])

    def test_default_cache_is_machine_local_and_independent_of_database(self):
        with patch("photography_lib.config.sys.platform", "win32"), \
             patch.dict(os.environ, {"LOCALAPPDATA": str(self.base / "local")}):
            self.assertEqual(Config(self.path).model_cache_root, self.base / "local" / "SmartAlbums" / "models")
        with patch("photography_lib.config.sys.platform", "win32"), \
             patch.dict(os.environ, {"LOCALAPPDATA": ""}), patch.object(Path, "home", return_value=self.base / "home"):
            self.assertEqual(Config(self.path).model_cache_root,
                             self.base / "home" / "AppData" / "Local" / "SmartAlbums" / "models")
        with patch("photography_lib.config.sys.platform", "linux"), \
             patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.base / "xdg")}):
            self.assertEqual(Config(self.path).model_cache_root, self.base / "xdg" / "smart-albums" / "models")
        with patch("photography_lib.config.sys.platform", "darwin"), patch.object(Path, "home", return_value=self.base):
            self.assertEqual(Config(self.path).model_cache_root, self.base / "Library" / "Caches" / "SmartAlbums" / "models")
        self.assertEqual(list(self.base.iterdir()), [])

    def test_config_retains_thumbnail_validation_and_error_details(self):
        for options in ({"thumbnail_size": 12}, {"thumbnail_quality": 100}):
            with self.subTest(options=options), self.assertRaises(PhotographyError):
                Config(self.path, **options)
        self.assertEqual(PhotographyError("CODE", "message", "scan", details={"key": 1}).to_dict(),
                         {"code": "CODE", "message": "message", "scan_id": "scan", "details": {"key": 1}})

    def test_constructor_is_not_an_implicit_create(self):
        with self.assertRaises(PhotographyError):
            SQLiteStorage(self.path)
        self.assertFalse(self.path.exists())

    def test_create_returns_open_writable_v8_album_with_exact_tables(self):
        with SQLiteStorage.create(self.path) as store:
            self.assertTrue(store.writable)
            self.assertEqual(store.database_path, self.path)
            self.assertEqual(store.db.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(store.db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(store.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            tables = {row[0] for row in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            self.assertEqual(tables, set(REQUIRED_COLUMNS))
            self.assertEqual(len(tables), 11)
            self.assertEqual(store.photos(), [])
            album = store.album()
            self.assertEqual(album["name"], self.path.stem)
            self.assertEqual(album["database_path"], str(self.path))
            self.assertEqual(str(UUID(album["id"])), album["id"])
            self.assertTrue(album["created_at"])
        self.assertIsNone(store.db)
        self.assertEqual(set(self.snapshot()), {self.path.name})

    def test_create_refuses_existing_files_without_mutation(self):
        self.path.write_bytes(b"existing non-SQLite data")
        before = self.snapshot()
        with self.assertRaises(PhotographyError) as failure:
            SQLiteStorage.create(self.path)
        self.assertEqual(failure.exception.code, "DATABASE_EXISTS")
        self.assertEqual(before, self.snapshot())

    def test_create_is_exclusive_under_racing_creators(self):
        def create():
            try:
                with SQLiteStorage.create(self.path) as store:
                    return store.album()["id"]
            except PhotographyError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda unused: create(), range(2)))
        self.assertEqual(results.count("DATABASE_EXISTS"), 1)
        with SQLiteStorage.open(self.path) as store:
            self.assertIn(store.album()["id"], results)

    def test_create_failure_removes_only_newly_owned_file(self):
        unrelated = self.base / "other.db"
        unrelated.write_bytes(b"untouched")
        with patch.object(SQLiteStorage, "_validate_format", side_effect=RuntimeError("initialization interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                SQLiteStorage.create(self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.base.iterdir()), [unrelated])
        self.assertEqual(unrelated.read_bytes(), b"untouched")

    def test_create_failure_does_not_delete_replacement_file(self):
        owned = self.base / "owned.sqlite"
        replacement = b"another owner's file"
        replaced = []
        def replace(database_path, *, writable):
            replaced.append(database_path)
            database_path.rename(owned)
            database_path.write_bytes(replacement)
            raise RuntimeError("replacement race")
        with patch.object(SQLiteStorage, "_connect", side_effect=replace):
            with self.assertRaisesRegex(RuntimeError, "replacement race"):
                SQLiteStorage.create(self.path)
        self.assertEqual(replaced[0].read_bytes(), replacement)
        self.assertFalse(self.path.exists())
        self.assertTrue(owned.exists())

    def test_create_never_overwrites_file_that_appears_before_publication(self):
        publish = storage_module._publish_new
        replacement = b"another process created this file"

        def race(temporary, destination):
            destination.write_bytes(replacement)
            publish(temporary, destination)

        with patch.object(storage_module, "_publish_new", side_effect=race), self.assertRaises(PhotographyError) as error:
            SQLiteStorage.create(self.path)
        self.assertEqual(error.exception.code, "DATABASE_EXISTS")
        self.assertEqual(self.path.read_bytes(), replacement)
        self.assertEqual(set(self.snapshot()), {self.path.name})

    def test_create_missing_parent_does_not_create_directory(self):
        target = self.base / "missing" / "album.db"
        with self.assertRaises(PhotographyError):
            SQLiteStorage.create(target)
        self.assertFalse(target.parent.exists())

    def test_open_missing_file_never_creates_anything(self):
        for writable in (False, True):
            with self.subTest(writable=writable), self.assertRaises(PhotographyError) as failure:
                SQLiteStorage.open(self.path, writable=writable)
            self.assertEqual(failure.exception.code, "DATABASE_NOT_FOUND")
        self.assertEqual(list(self.base.iterdir()), [])

    def test_open_existing_album_is_read_only_and_nonmutating_by_default(self):
        with SQLiteStorage.create(self.path) as store:
            store.put_photo(self.photo())
        before = self.snapshot()
        with SQLiteStorage.open(self.path) as store:
            self.assertFalse(store.writable)
            self.assertEqual(store.photo("photo_a"), self.photo())
            with store.read_snapshot():
                self.assertEqual(len(store.photos()), 1)
        self.assertEqual(before, self.snapshot())

    def test_open_writable_does_not_initialize_or_change_existing_file(self):
        with SQLiteStorage.create(self.path):
            pass
        before = self.snapshot()
        with SQLiteStorage.open(self.path, writable=True) as store:
            self.assertTrue(store.writable)
            self.assertEqual(store.photos(), [])
        self.assertEqual(before, self.snapshot())

    def test_open_rejects_old_foreign_and_corrupt_files_unchanged(self):
        for fixture in ("v7", "foreign", "invalid", "empty", "future"):
            path = self.base / (fixture + ".db")
            if fixture in ("v7", "foreign", "future"):
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute("CREATE TABLE user_data (payload TEXT)")
                    connection.execute("INSERT INTO user_data VALUES ('preserve me')")
                    if fixture == "v7":
                        connection.execute("PRAGMA user_version=7")
                    elif fixture == "future":
                        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                        connection.execute("PRAGMA user_version=99")
                    connection.commit()
            else:
                path.write_bytes(b"not a SQLite database" if fixture == "invalid" else b"")
            before = self.snapshot()
            for writable in (False, True):
                with self.subTest(fixture=fixture, writable=writable), self.assertRaises(PhotographyError):
                    SQLiteStorage.open(path, writable=writable)
                self.assertEqual(before, self.snapshot())

    def test_open_rejects_missing_table_and_columns_without_schema_repair(self):
        mutations = (
            "DROP TABLE thumbnails",
            "ALTER TABLE photos DROP COLUMN last_path_error",
            "ALTER TABLE image_embedding_profiles RENAME COLUMN profile_json TO wrong_json",
            "CREATE TABLE technical_placeholder (id TEXT)",
        )
        for number, mutation in enumerate(mutations):
            path = self.base / f"invalid-{number}.sqlite"
            with SQLiteStorage.create(path) as store:
                store.db.execute(mutation)
            before = self.snapshot()
            for writable in (False, True):
                with self.subTest(mutation=mutation, writable=writable), self.assertRaises(PhotographyError) as failure:
                    SQLiteStorage.open(path, writable=writable)
                self.assertEqual(failure.exception.code, "SCHEMA_INVALID")
                self.assertEqual(before, self.snapshot())

    def test_open_rejects_invalid_singleton_uuid_and_timestamp(self):
        changes = (
            "DELETE FROM album_metadata",
            "UPDATE album_metadata SET album_uuid='invalid'",
            "UPDATE album_metadata SET created_at='invalid'",
            "PRAGMA ignore_check_constraints=ON; INSERT INTO album_metadata SELECT 2,album_uuid,created_at FROM album_metadata",
        )
        for number, statements in enumerate(changes):
            path = self.base / f"metadata-{number}.sqlite"
            with SQLiteStorage.create(path) as store:
                store.db.executescript(statements)
            before = self.snapshot()
            with self.subTest(statements=statements), self.assertRaises(PhotographyError) as failure:
                SQLiteStorage.open(path)
            self.assertEqual(failure.exception.code, "SCHEMA_INVALID")
            self.assertEqual(before, self.snapshot())

    def test_read_only_methods_fail_clearly_without_writes(self):
        with SQLiteStorage.create(self.path) as store:
            store.put_photo(self.photo())
        before = self.snapshot()
        with SQLiteStorage.open(self.path) as store:
            operations = (
                store.assert_writable,
                lambda: store.put_photo(self.photo()),
                lambda: store.put_thumbnail(self.photo(), self.preview()),
                lambda: store.start_scan("scan", str(self.base)),
                lambda: store.event("scan", "error", None, "path"),
                lambda: store.finish_scan("scan", {"status": "completed"}),
            )
            for operation in operations:
                with self.subTest(operation=operation), self.assertRaises(PhotographyError) as failure:
                    operation()
                self.assertEqual(failure.exception.code, "STORAGE_READ_ONLY")
            for context in (store.transaction, store.savepoint):
                with self.subTest(context=context), self.assertRaises(PhotographyError) as failure:
                    with context():
                        self.fail("A read-only writer context was entered")
                self.assertEqual(failure.exception.code, "STORAGE_READ_ONLY")
            with self.assertRaises(sqlite3.OperationalError):
                store.db.execute("DELETE FROM photos")
        self.assertEqual(before, self.snapshot())

    def test_photo_columns_round_trip_without_original_io_or_legacy_aliases(self):
        photo = self.photo(last_ingest_error={}, last_path_error={"code": "UNAVAILABLE"},
                           original_status="unavailable", path_updated_at="2026-09-06T01:00:00+00:00")
        with SQLiteStorage.create(self.path) as store:
            with patch.object(Path, "open", side_effect=AssertionError("original I/O")), \
                 patch.object(Path, "stat", side_effect=AssertionError("original stat")):
                store.put_photo(photo)
                self.assertEqual(store.photo(photo["photo_id"]), photo)
                self.assertEqual(store.photos(), [photo])
            row = store.db.execute("SELECT * FROM photos").fetchone()
            self.assertIsInstance(row["size_bytes"], int)
            self.assertIsInstance(row["mtime_ns"], int)
            self.assertEqual(row["original_relative_path"], "../nonexistent-originals/猫.jpg")
            self.assertNotIn("data_json", row.keys())
            for alias in ("state", "original_path", "relative_path", "content_hash", "library_id"):
                self.assertNotIn(alias, store.photo(photo["photo_id"]))
        self.assertFalse((self.base / "nonexistent-originals").exists())

    def test_photo_upsert_preserves_values_and_allows_ambiguous_relative_paths(self):
        with SQLiteStorage.create(self.path) as store:
            store.put_photo(self.photo("photo_b"))
            store.put_photo(self.photo("photo_a"))
            replacement = self.photo("photo_a", original_absolute_path="Z:\\relocated\\猫.jpg",
                                     original_relative_path=None, ingest_state="error", size_bytes=456,
                                     last_ingest_error={"code": "SYNTHETIC"})
            store.put_photo(replacement)
            self.assertEqual(store.photo("photo_a"), replacement)
            self.assertEqual([photo["photo_id"] for photo in store.photos()], ["photo_a", "photo_b"])

    def test_photo_optional_fields_default_none_and_core_fields_are_required(self):
        photo = self.photo()
        for field in PHOTO_OPTIONAL_FIELDS:
            photo.pop(field)
        with SQLiteStorage.create(self.path) as store:
            store.put_photo(photo)
            expected = {**photo, **dict.fromkeys(PHOTO_OPTIONAL_FIELDS)}
            self.assertEqual(store.photo("photo_a"), expected)
            for field in ("photo_id", "metadata", "content_version", "original_status", "size_bytes"):
                incomplete = dict(photo)
                incomplete.pop(field)
                with self.subTest(field=field), self.assertRaises(PhotographyError):
                    store.put_photo(incomplete)

    def test_thumbnail_and_preview_contract_remains_compatible(self):
        photo, data = self.photo(), self.preview()
        with SQLiteStorage.create(self.path) as store:
            store.put_photo(photo)
            store.put_thumbnail(photo, data)
            self.assertEqual(stored_preview(store.photo("photo_a"), store), data)
            self.assertNotIn("data", store.thumbnail("photo_a", include_data=False))
            self.assertEqual(store.thumbnail("photo_a")["image_hash"], hashlib.sha256(data).hexdigest())
            with self.assertRaises(sqlite3.IntegrityError):
                store.put_thumbnail(self.photo("no-photo"), data)

    def test_transactions_savepoints_and_snapshots_preserve_atomicity(self):
        with SQLiteStorage.create(self.path) as store:
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                with store.transaction():
                    store.put_photo(self.photo())
                    raise RuntimeError("rollback")
            self.assertEqual(store.photos(), [])
            with store.transaction():
                store.put_photo(self.photo())
                with self.assertRaisesRegex(RuntimeError, "savepoint"):
                    with store.savepoint():
                        store.put_photo(self.photo("rolled-back"))
                        raise RuntimeError("savepoint")
                with store.transaction():
                    store.put_photo(self.photo("photo_b"))
                with store.read_snapshot():
                    self.assertEqual(len(store.photos()), 2)
            self.assertEqual([photo["photo_id"] for photo in store.photos()], ["photo_a", "photo_b"])

    def test_scan_source_paths_events_and_pagination(self):
        with SQLiteStorage.create(self.path) as store:
            store.put_photo(self.photo())
            with store.transaction():
                store.start_scan("scan", str(self.base), "../originals")
                store.event("scan", "added", "photo_a", "originals/猫.jpg")
                store.event("scan", "unchanged", "photo_a", "originals/猫.jpg")
                store.event("scan", "error", None, "bad.jpg", {})
                store.finish_scan("scan", {"status": "completed", "added": 1})
            scan = store.scan("scan")
            self.assertEqual(scan["source_absolute_path"], str(self.base))
            self.assertEqual(scan["source_relative_path"], "../originals")
            self.assertEqual(scan["result"], {"status": "completed", "added": 1})
            self.assertTrue(scan["completed_at"])
            first = store.events("scan", limit=1)
            rest = store.events("scan", after=first["next_cursor"])
            self.assertEqual(len(first["items"]) + len(rest["items"]), 3)
            changed = store.events("scan", changes_only=True)
            self.assertEqual([item["kind"] for item in changed["items"]], ["added", "error"])
            self.assertEqual(changed["items"][-1]["error"], {})
            store.start_scan("cross-drive", str(self.base))
            self.assertIsNone(store.scan("cross-drive")["source_relative_path"])
            for invalid_limit in (0, 1001, True, "5"):
                with self.subTest(limit=invalid_limit), self.assertRaises(PhotographyError):
                    store.events("scan", limit=invalid_limit)
            for invalid_after in (-1, True, "1"):
                with self.subTest(after=invalid_after), self.assertRaises(PhotographyError):
                    store.events("scan", after=invalid_after)

    def test_backup_preserves_identity_and_content_from_read_only_source(self):
        data, photo = self.preview(), self.photo()
        with SQLiteStorage.create(self.path) as store:
            store.put_photo(photo)
            store.put_thumbnail(photo, data)
            album_id = store.album()["id"]
        backup = self.base / "backup.sqlite3"
        before = self.path.read_bytes()
        with SQLiteStorage.open(self.path) as source:
            result = source.backup(backup)
            self.assertEqual(result["output"], str(backup))
            self.assertEqual(result["album"]["id"], album_id)
            self.assertGreater(result["size_bytes"], 0)
        with SQLiteStorage.open(backup) as stored:
            self.assertEqual(stored.album()["id"], album_id)
            self.assertEqual(stored.album()["name"], "backup")
            self.assertEqual(stored.photo("photo_a"), photo)
            self.assertEqual(stored_preview(photo, stored), data)
            self.assertEqual(stored.db.execute("PRAGMA user_version").fetchone()[0], 8)
        self.assertEqual(self.path.read_bytes(), before)

    def test_backup_does_not_overwrite_destination_or_source(self):
        output = self.base / "exists.db"
        output.write_bytes(b"keep")
        with SQLiteStorage.create(self.path) as store:
            before = self.snapshot()
            for destination in (output, self.path):
                with self.subTest(destination=destination), self.assertRaises(PhotographyError) as failure:
                    store.backup(destination)
                self.assertEqual(failure.exception.code, "DATABASE_EXISTS")
                self.assertEqual(before, self.snapshot())

    def test_backup_never_opens_a_racing_public_destination_for_writing(self):
        output = self.base / "racing.sqlite"
        publish = storage_module._publish_new
        with SQLiteStorage.create(self.path) as store:
            def race(temporary, destination):
                with closing(sqlite3.connect(destination)) as other:
                    other.execute("CREATE TABLE important(payload TEXT)")
                    other.execute("INSERT INTO important VALUES ('preserve this data')")
                    other.commit()
                publish(temporary, destination)

            with patch.object(storage_module, "_publish_new", side_effect=race), self.assertRaises(PhotographyError) as error:
                store.backup(output)
            self.assertEqual(error.exception.code, "DATABASE_EXISTS")
        with closing(sqlite3.connect(output)) as db:
            self.assertEqual(db.execute("SELECT payload FROM important").fetchone()[0], "preserve this data")
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='album_metadata'").fetchone())
        self.assertEqual(set(self.snapshot()), {self.path.name, output.name})

    def test_backup_failure_cleans_owned_output_only(self):
        class InterruptBackup(sqlite3.Connection):
            def backup(self, target, **kwargs):
                target.execute("CREATE TABLE partial (payload TEXT)")
                raise KeyboardInterrupt("synthetic interruption")
        output = self.base / "interrupted.db"
        with SQLiteStorage.create(self.path) as store:
            store.db.close()
            store.db = sqlite3.connect(self.path, factory=InterruptBackup)
            before = self.path.read_bytes()
            with self.assertRaisesRegex(KeyboardInterrupt, "synthetic"):
                store.backup(output)
            self.assertFalse(output.exists())
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(set(self.snapshot()), {self.path.name})

    def test_backup_rejects_active_transaction_instead_of_hanging(self):
        output = self.base / "busy.sqlite"
        with SQLiteStorage.create(self.path) as store, store.transaction():
            store.put_photo(self.photo())
            with self.assertRaises(PhotographyError) as failure:
                store.backup(output)
            self.assertEqual(failure.exception.code, "STORAGE_BUSY")
            self.assertFalse(output.exists())

    def test_two_album_files_keep_photos_and_identity_isolated(self):
        second_path = self.base / "second.db"
        with SQLiteStorage.create(self.path) as first, SQLiteStorage.create(second_path) as second:
            first.put_photo(self.photo("same-id", metadata={"album": "first"}))
            second.put_photo(self.photo("same-id", metadata={"album": "second"}))
            self.assertNotEqual(first.album()["id"], second.album()["id"])
        with SQLiteStorage.open(self.path) as first, SQLiteStorage.open(second_path) as second:
            self.assertEqual(first.photo("same-id")["metadata"], {"album": "first"})
            self.assertEqual(second.photo("same-id")["metadata"], {"album": "second"})

    def test_album_uuid_survives_rename_while_display_name_follows_filename(self):
        with SQLiteStorage.create(self.path) as store:
            album_id = store.album()["id"]
        renamed = self.base / "renamed.db"
        self.path.rename(renamed)
        with SQLiteStorage.open(renamed) as store:
            self.assertEqual(store.album()["id"], album_id)
            self.assertEqual(store.album()["name"], "renamed")
            self.assertEqual(store.album()["database_path"], str(renamed))


if __name__ == "__main__":
    unittest.main()
