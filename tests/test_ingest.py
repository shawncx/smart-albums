from __future__ import annotations

import hashlib
import importlib
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))
from photography_lib import Config, ingestion
from photography_lib.config import PhotographyError
from photography_lib.sqlite_storage import SQLiteStorage

ingest_module = importlib.import_module("photography_lib.ingest")
images_module = importlib.import_module("photography_lib.images")


class IngestionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="portable-ingestion-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "photos"
        self.source.mkdir()
        self.database = self.base / "album.sqlite"
        self.config = Config(self.database, model_cache_root=self.base / "cache", thumbnail_size=256)
        SQLiteStorage.create(self.database).close()

    def image(self, name="photo.jpg", size=(640, 320), color="navy", **kwargs):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, color).save(path, **kwargs)
        return path

    def records(self, database=None):
        with SQLiteStorage.open(database or self.database) as store:
            return store.photos()

    def thumbnail(self, photo, database=None):
        with SQLiteStorage.open(database or self.database) as store:
            return store.thumbnail(photo["photo_id"])

    def test_first_import_and_repeat_preserve_original_and_preview(self):
        path = self.image("nested/photo.JPG")
        before = path.read_bytes(), path.stat().st_mtime_ns
        first = ingestion(self.source, config=self.config)
        photo = self.records()[0]
        thumbnail = self.thumbnail(photo)
        self.assertEqual((first["added"], first["scanned"], first["model_calls"]), (1, 1, 0))
        self.assertEqual(photo["original_absolute_path"], str(path))
        self.assertEqual(photo["original_relative_path"], "photos/nested/photo.JPG")
        self.assertEqual(photo["content_version"], hashlib.sha256(before[0]).hexdigest())
        with Image.open(io.BytesIO(thumbnail["data"])) as preview:
            self.assertEqual(preview.size, (256, 128))
        with patch.object(ingest_module, "inspect_photo", side_effect=AssertionError("No repeat decode")):
            repeated = ingestion(self.source, config=self.config)
        self.assertEqual((repeated["unchanged"], repeated["changed_photo_ids"]), (1, []))
        self.assertEqual(repeated["album"]["id"], first["album"]["id"])
        self.assertEqual(self.thumbnail(photo), thumbnail)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(repeated["index_prompt"]["photo_ids"], first["successful_photo_ids"])

    def test_scan_lookup_work_is_linear_for_new_and_existing_photos(self):
        for size in (20, 40):
            source = self.base / f"scale-{size}"
            source.mkdir()
            data = self.image(size=(16, 12)).read_bytes()
            for index in range(size):
                (source / f"{index:03}.jpg").write_bytes(data)
            config = Config(self.base / f"scale-{size}.sqlite", model_cache_root=self.base / "cache")
            with SQLiteStorage.create(config.database_path) as store:
                for repeat in (False, True):
                    with self.subTest(size=size, repeat=repeat), \
                         patch.object(store, "photos", wraps=store.photos) as full_reads, \
                         patch.object(store, "photo_locations", wraps=store.photo_locations) as refreshes, \
                         patch.object(store, "_photo", wraps=store._photo) as materialized, \
                         patch.object(ingest_module, "_candidates", wraps=ingest_module._candidates) as candidates:
                        result = ingestion(source, config=config, storage=store)
                        self.assertEqual(result["unchanged" if repeat else "added"], size)
                        self.assertEqual(full_reads.call_count, 1)
                        self.assertEqual(refreshes.call_count, 0)
                        self.assertEqual(materialized.call_count, 3 * size if repeat else 0)
                        self.assertEqual(sum(len(call.args[1]) for call in candidates.call_args_list),
                                         2 * size if repeat else 0)

    def test_concurrent_insert_of_normalized_relative_alias_does_not_duplicate_identity(self):
        self.image()
        prepare = ingest_module._prepare
        with SQLiteStorage.open(self.database, writable=True) as store, \
             SQLiteStorage.open(self.database, writable=True) as writer:
            def insert_after_read(*args):
                result = prepare(*args)
                photo, unused, unused_changed, thumbnail, *unused_rest = result
                with writer.transaction():
                    inserted = {**photo, "photo_id": "concurrent-photo",
                                "original_absolute_path": str(self.base / "no-longer-here.jpg"),
                                "original_relative_path": "photos/../photos/./photo.jpg"}
                    writer.put_photo(inserted)
                    writer.put_thumbnail(inserted, thumbnail)
                return result

            with patch.object(ingest_module, "_prepare", side_effect=insert_after_read):
                result = ingestion(self.source, config=self.config, storage=store)
            self.assertEqual((result["added"], result["failed"]), (0, 1))
            self.assertEqual(result["errors"][0]["code"], "PHOTO_PATH_CHANGED")
            self.assertEqual([row["photo_id"] for row in store.photos()], ["concurrent-photo"])
            repeated = ingestion(self.source, config=self.config, storage=store)
            self.assertEqual(repeated["successful_photo_ids"], ["concurrent-photo"])

    def test_same_connection_interleaved_writes_only_refresh_touched_locations(self):
        for size in (20, 40):
            source = self.base / f"interleaved-{size}"
            source.mkdir()
            data = self.image(size=(16, 12)).read_bytes()
            for index in range(size):
                (source / f"{index:03}.jpg").write_bytes(data)
            config = Config(self.base / f"interleaved-{size}.sqlite", model_cache_root=self.base / "cache")
            with SQLiteStorage.create(config.database_path) as store:
                ingestion(source, config=config, storage=store)
                unrelated = {**store.photos()[0], "photo_id": "unrelated",
                             "original_absolute_path": str(self.base / "elsewhere.jpg"),
                             "original_relative_path": None}
                store.put_photo(unrelated)
                prepare = ingest_module._prepare

                def unrelated_writes(*args):
                    prepared = prepare(*args)
                    scan = store.db.execute("SELECT scan_id FROM scans WHERE status='running'").fetchone()[0]
                    store.event(scan, "diagnostic", None, str(source))
                    store.put_photo({**unrelated, "original_status": "not_checked"})
                    return prepared

                with self.subTest(size=size), patch.object(ingest_module, "_prepare", side_effect=unrelated_writes), \
                     patch.object(store, "photos", wraps=store.photos) as full_reads, \
                     patch.object(store, "photo_locations", wraps=store.photo_locations) as locations:
                    result = ingestion(source, config=config, storage=store)
                self.assertEqual((result["unchanged"], result["failed"]), (size, 0))
                self.assertEqual(full_reads.call_count, 1)
                self.assertEqual(locations.call_count, size)
                self.assertTrue(all(call.args == ({"unrelated"},) for call in locations.call_args_list))

    def test_external_insert_and_relink_batch_reconciles_once(self):
        for size in (20, 40):
            source = self.base / f"external-{size}"
            source.mkdir()
            data = self.image(size=(16, 12)).read_bytes()
            for index in range(size):
                (source / f"{index:03}.jpg").write_bytes(data)
            config = Config(self.base / f"external-{size}.sqlite", model_cache_root=self.base / "cache")
            with SQLiteStorage.create(config.database_path) as store, \
                 SQLiteStorage.open(config.database_path, writable=True) as writer:
                ingestion(source, config=config, storage=store)
                records = store.photos()
                for old in records:
                    writer.put_photo({**old, "original_absolute_path": str(self.base / old["photo_id"]),
                                      "original_relative_path": None})
                walk = ingest_module._walk

                def external_batch(*args):
                    with writer.transaction():
                        for old in records:
                            writer.put_photo(old)
                        template = records[0]
                        for index in range(size):
                            path = source / f"new-{index:03}.jpg"
                            path.write_bytes(data)
                            photo = {**template, "photo_id": f"external-{index:03}",
                                     "original_absolute_path": str(self.base / "absent" / path.name),
                                     "original_relative_path": f"{source.name}/../{source.name}/./{path.name}"}
                            writer.put_photo(photo)
                            writer.put_thumbnail(photo, data)
                    yield from walk(*args)

                with self.subTest(size=size), patch.object(ingest_module, "_walk", side_effect=external_batch), \
                     patch.object(store, "photos", wraps=store.photos) as full_reads, \
                     patch.object(store, "photo_locations", wraps=store.photo_locations) as locations:
                    result = ingestion(source, config=config, storage=store)
                self.assertEqual((result["added"], result["unchanged"], result["failed"]), (0, 2 * size, 0))
                self.assertEqual(full_reads.call_count, 1)
                self.assertEqual(locations.call_count, 1)
                self.assertEqual(locations.call_args.args, ())
                self.assertEqual(len(store.photos()), 2 * size)

    def test_untracked_same_connection_relink_cannot_hide_behind_scan_writes(self):
        self.image()
        ingestion(self.source, config=self.config)
        prepare = ingest_module._prepare
        with SQLiteStorage.open(self.database, writable=True) as store:
            old = store.photos()[0]
            store.put_photo({**old, "original_absolute_path": str(self.base / "elsewhere.jpg"),
                             "original_relative_path": None})

            def relink_after_read(*args):
                prepared = prepare(*args)
                store.db.execute("UPDATE photos SET original_relative_path='photos/../photos/photo.jpg' WHERE photo_id=?",
                                 (old["photo_id"],))
                scan = store.db.execute("SELECT scan_id FROM scans WHERE status='running'").fetchone()[0]
                store.event(scan, "diagnostic", None, str(self.source))
                return prepared

            with patch.object(ingest_module, "_prepare", side_effect=relink_after_read):
                result = ingestion(self.source, config=self.config, storage=store)
            self.assertEqual(result["errors"][0]["code"], "PHOTO_PATH_CHANGED")
            self.assertEqual([photo["photo_id"] for photo in store.photos()], [old["photo_id"]])

    def test_lookup_refreshes_relinks_between_files_and_preserves_conflicts(self):
        first = self.image("a.jpg")
        second = self.image("b.jpg")
        ingestion(self.source, config=self.config)
        with SQLiteStorage.open(self.database, writable=True) as store, \
             SQLiteStorage.open(self.database, writable=True) as writer:
            records = {Path(row["original_absolute_path"]).name: row for row in store.photos()}

            def relink_between_files(*args):
                yield first
                with writer.transaction():
                    writer.put_photo({**records["a.jpg"], "original_absolute_path": str(second.parent / "." / second.name),
                                      "original_relative_path": "photos/../photos/b.jpg"})
                yield second

            with patch.object(ingest_module, "_walk", side_effect=relink_between_files), \
                 patch.object(store, "photo_locations", wraps=store.photo_locations) as refreshes:
                result = ingestion(self.source, config=self.config, storage=store)
            self.assertEqual((result["unchanged"], result["failed"], result["added"]), (1, 1, 0))
            self.assertEqual(result["errors"][0]["code"], "PHOTO_PATH_CONFLICT")
            self.assertEqual(refreshes.call_count, 1)
            self.assertEqual(len(store.photos()), 2)

    def test_concurrent_relink_removing_both_aliases_is_not_overwritten(self):
        self.image()
        ingestion(self.source, config=self.config)
        prepare = ingest_module._prepare
        with SQLiteStorage.open(self.database, writable=True) as writer:
            old = writer.photos()[0]
            relocated = {**old, "original_absolute_path": str(self.base / "elsewhere.jpg"),
                         "original_relative_path": "elsewhere.jpg"}

            def relink_after_read(*args):
                prepared = prepare(*args)
                writer.put_photo(relocated)
                return prepared

            with patch.object(ingest_module, "_prepare", side_effect=relink_after_read):
                result = ingestion(self.source, config=self.config)
            self.assertEqual(result["errors"][0]["code"], "PHOTO_PATH_CHANGED")
            self.assertEqual(writer.photo(old["photo_id"])["original_absolute_path"], relocated["original_absolute_path"])
            self.assertEqual(len(writer.photos()), 1)

    def test_arbitrary_rename_does_not_reuse_content_identity(self):
        path = self.image()
        ingestion(self.source, config=self.config)
        old = self.records()[0]
        path.rename(self.source / "renamed.jpg")
        result = ingestion(self.source, config=self.config)
        self.assertEqual((result["added"], result["missing"]), (1, 1))
        self.assertNotEqual(result["successful_photo_ids"], [old["photo_id"]])

    def test_changed_photo_keeps_id_and_reports_new_input(self):
        path = self.image()
        ingestion(self.source, config=self.config)
        old = self.records()[0]
        self.image(color="orange")
        os.utime(path, ns=(path.stat().st_atime_ns, old["mtime_ns"] + 1_000_000_000))
        result = ingestion(self.source, config=self.config)
        current = self.records()[0]
        self.assertEqual((result["updated"], current["photo_id"]), (1, old["photo_id"]))
        self.assertNotEqual(current["content_version"], old["content_version"])
        self.assertEqual(result["index_prompt"]["photo_ids"], [old["photo_id"]])

    def test_missing_and_restore_preserve_identity(self):
        path = self.image()
        data = path.read_bytes()
        ingestion(self.source, config=self.config)
        old = self.records()[0]
        path.unlink()
        self.assertEqual(ingestion(self.source, config=self.config)["missing"], 1)
        self.assertEqual(ingestion(self.source, config=self.config)["missing"], 0)
        path.write_bytes(data)
        result = ingestion(self.source, config=self.config)
        self.assertEqual((result["restored"], result["changed_photo_ids"]), (1, []))
        self.assertEqual(self.records()[0]["photo_id"], old["photo_id"])
        self.assertEqual(self.records()[0]["original_status"], "available")

    def test_other_directory_records_are_not_marked_missing(self):
        self.image()
        ingestion(self.source, config=self.config)
        other = self.base / "other"
        other.mkdir()
        Image.new("RGB", (80, 60), "red").save(other / "second.jpg")
        result = ingestion(other, config=self.config)
        self.assertEqual((result["added"], result["missing"]), (1, 0))
        self.assertEqual(len(self.records()), 2)
        overlap = ingestion(self.base, config=self.config)
        self.assertEqual((overlap["added"], overlap["unchanged"]), (0, 2))

    def test_identical_files_are_not_hash_deduplicated(self):
        path = self.image()
        (self.source / "copy.jpg").write_bytes(path.read_bytes())
        result = ingestion(self.source, config=self.config)
        self.assertEqual(result["added"], 2)
        self.assertEqual(len({p["photo_id"] for p in self.records()}), 2)
        self.assertEqual(len({p["content_version"] for p in self.records()}), 1)

    def test_database_can_be_inside_photo_directory_and_cache_is_excluded(self):
        self.image()
        database = self.source / "inside.sqlite"
        SQLiteStorage.create(database).close()
        cache = self.source / "model-cache"
        cache.mkdir()
        Image.new("RGB", (40, 40), "white").save(cache / "not-an-original.jpg")
        config = Config(database, model_cache_root=cache)
        result = ingestion(self.source, config=config)
        self.assertEqual((result["scanned"], result["added"]), (1, 1))
        self.assertEqual(self.records(database)[0]["original_relative_path"], "photo.jpg")

    def test_cross_drive_relative_failure_is_warning_not_import_failure(self):
        self.image()
        with patch.object(ingest_module, "relative_original_path",
                          return_value=(None, {"code": "RELATIVE_PATH_UNAVAILABLE", "message": "Different drive"})):
            result = ingestion(self.source, config=self.config)
        self.assertEqual(result["added"], 1)
        self.assertIsNone(self.records()[0]["original_relative_path"])
        self.assertTrue(result["warnings"])

    def test_move_album_and_photos_then_rescan_reuses_id_and_preview(self):
        path = self.image()
        first = ingestion(self.source, config=self.config)
        old = self.records()[0]
        thumbnail = self.thumbnail(old)
        moved = self.base / "moved"
        moved.mkdir()
        new_database = moved / self.database.name
        shutil.move(self.database, new_database)
        new_source = moved / "photos"
        shutil.move(self.source, new_source)
        config = Config(new_database, model_cache_root=self.base / "cache", thumbnail_size=256)
        result = ingestion(new_source, config=config)
        current = self.records(new_database)[0]
        self.assertEqual((result["added"], result["unchanged"], result["changed_photo_ids"]), (0, 1, []))
        self.assertEqual(result["album"]["id"], first["album"]["id"])
        self.assertEqual(current["photo_id"], old["photo_id"])
        self.assertEqual(current["original_absolute_path"], str(new_source / path.name))
        self.assertEqual(self.thumbnail(current, new_database), thumbnail)

    def test_missing_absolute_with_available_relative_elsewhere_is_not_missing(self):
        path = self.image()
        ingestion(self.source, config=self.config)
        backup = self.base / "backup.jpg"
        shutil.copyfile(path, backup)
        with SQLiteStorage.open(self.database, writable=True) as store:
            old = store.photos()[0]
            store.put_photo({**old, "original_relative_path": "backup.jpg"})
        path.unlink()
        result = ingestion(self.source, config=self.config)
        self.assertEqual(result["missing"], 0)
        self.assertEqual(self.records()[0]["original_absolute_path"], str(backup))

    def test_existing_absolute_wins_over_a_relative_copy(self):
        path = self.image()
        ingestion(self.source, config=self.config)
        copy = self.base / "copies"
        copy.mkdir()
        shutil.copyfile(path, copy / path.name)
        with SQLiteStorage.open(self.database, writable=True) as store:
            old = store.photos()[0]
            store.put_photo({**old, "original_relative_path": "copies/photo.jpg"})
        result = ingestion(copy, config=self.config)
        self.assertEqual((result["failed"], result["added"]), (1, 0))
        self.assertEqual(result["errors"][0]["code"], "PHOTO_PATH_CONFLICT")
        self.assertEqual(self.records()[0]["original_absolute_path"], str(path))

    def test_bad_photo_is_isolated_and_success_scope_prompts(self):
        path = self.image()
        ingestion(self.source, config=self.config)
        old = self.records()[0]
        path.write_bytes(b"broken")
        self.image("valid.jpg", color="green")
        result = ingestion(self.source, config=self.config)
        self.assertEqual((result["status"], result["failed"], result["added"]), ("partial", 1, 1))
        self.assertEqual(result["index_prompt"]["photo_ids"], result["successful_photo_ids"])
        by_id = {p["photo_id"]: p for p in self.records()}
        self.assertEqual(by_id[old["photo_id"]]["ingest_state"], "error")
        self.assertEqual(by_id[old["photo_id"]]["content_version"], old["content_version"])

    def test_preview_write_failure_rolls_back_new_photo(self):
        self.image()
        with patch.object(SQLiteStorage, "put_thumbnail", side_effect=PhotographyError("TEST_ERROR", "Save failed")):
            result = ingestion(self.source, config=self.config)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.records(), [])
        self.assertIsNone(result["index_prompt"])

    def test_original_reads_occur_outside_write_transaction(self):
        self.image()
        original = ingest_module.inspect_photo
        with SQLiteStorage.open(self.database, writable=True) as store:
            def check(*args, **kwargs):
                self.assertFalse(store.db.in_transaction)
                return original(*args, **kwargs)
            with patch.object(ingest_module, "inspect_photo", side_effect=check):
                self.assertEqual(ingestion(self.source, config=self.config, storage=store)["added"], 1)

    def test_concurrent_path_repair_is_not_overwritten_by_ingestion(self):
        path = self.image()
        ingestion(self.source, config=self.config)
        self.image(size=(300, 100), color="red")
        original_prepare = ingest_module._prepare
        with SQLiteStorage.open(self.database, writable=True) as store:
            old = store.photos()[0]
            relocated = str(self.base / "concurrent-location.jpg")

            def change_after_read(*args):
                result = original_prepare(*args)
                with store.transaction():
                    store.put_photo({**store.photo(old["photo_id"]), "original_absolute_path": relocated})
                return result

            with patch.object(ingest_module, "_prepare", side_effect=change_after_read):
                result = ingestion(self.source, config=self.config, storage=store)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["errors"][0]["code"], "PHOTO_PATH_CHANGED")
            self.assertEqual(store.photo(old["photo_id"])["original_absolute_path"], relocated)
            self.assertEqual(store.photo(old["photo_id"])["content_version"], old["content_version"])

    def test_changing_source_before_save_is_not_persisted_as_success(self):
        path = self.image()
        original = ingest_module._prepare

        def change_after_read(*args):
            result = original(*args)
            path.write_bytes(b"changed during import")
            return result

        with patch.object(ingest_module, "_prepare", side_effect=change_after_read):
            result = ingestion(self.source, config=self.config)
        self.assertEqual((result["failed"], result["added"]), (1, 0))
        self.assertEqual(result["errors"][0]["code"], "FILE_CHANGED_DURING_SCAN")
        self.assertEqual(self.records(), [])

    def test_incomplete_scan_does_not_mark_missing(self):
        first = self.image()
        second = self.image("second.jpg")
        ingestion(self.source, config=self.config)
        second.unlink()
        def broken(*args):
            yield first
            raise PermissionError("Synthetic enumeration error")
        with patch.object(ingest_module, "_walk", side_effect=broken), self.assertRaises(PhotographyError) as error:
            ingestion(self.source, config=self.config)
        self.assertEqual(error.exception.code, "SCAN_INCOMPLETE")
        self.assertTrue(all(p["original_status"] == "available" for p in self.records()))
        with SQLiteStorage.open(self.database) as store:
            self.assertEqual(store.scan(error.exception.scan_id)["result"]["missing"], 0)

    def test_orientation_small_photos_and_supported_formats(self):
        exif = Image.Exif()
        exif[274] = 6
        self.image(exif=exif)
        for extension in ("png", "webp", "tif", "bmp"):
            self.image("small." + extension, size=(40, 20))
        result = ingestion(self.source, config=self.config)
        self.assertEqual((result["added"], result["failed"]), (5, 0))
        for photo in self.records():
            thumb = self.thumbnail(photo)
            expected = (128, 256) if photo["original_absolute_path"].endswith("photo.jpg") else (40, 20)
            self.assertEqual((thumb["width"], thumb["height"]), expected)

    def test_open_does_not_create_missing_database_and_invalid_roots_do_not_write(self):
        missing = self.base / "does-not-exist.sqlite"
        self.image()
        with self.assertRaises(PhotographyError):
            ingestion(self.source, config=Config(missing))
        self.assertFalse(missing.exists())
        for path in ("relative", self.base / "absent", self.database):
            with self.subTest(path=path), self.assertRaises(PhotographyError):
                ingestion(path, config=self.config)
        with SQLiteStorage.open(self.database) as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM scans").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
