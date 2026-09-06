from __future__ import annotations

import hashlib
import io
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageCms

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))
from photography_lib import Config, ingest
from photography_lib.config import PhotographyError
from photography_lib.sqlite_storage import SQLiteStorage

ingest_module = importlib.import_module("photography_lib.ingest")
images_module = importlib.import_module("photography_lib.images")


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="photography-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.photos = self.base / "照片库"
        self.photos.mkdir()
        self.state = self.base / "state"
        self.config = Config(self.state, thumbnail_size=256)

    def make_photo(self, name="image.jpg", color="navy", size=(640, 320), **save_options):
        path = self.photos / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, color).save(path, **save_options)
        return path

    def records(self, library_id):
        with SQLiteStorage(self.state) as store:
            return store.photos_for_library(library_id)

    def preview(self, photo):
        with SQLiteStorage(self.state) as store:
            return store.thumbnail(photo["photo_id"])

    def test_first_import_and_second_scan_are_incremental_and_read_only(self):
        source = self.make_photo("子目录/照片.JPG")
        before = source.read_bytes(), source.stat().st_mtime_ns
        first = ingest(self.photos, config=self.config)
        self.assertEqual((first["added"], first["scanned"], first["failed"]), (1, 1, 0))
        photo = self.records(first["library_id"])[0]
        thumb = self.preview(photo)
        with Image.open(io.BytesIO(thumb["data"])) as preview:
            self.assertEqual(preview.size, (256, 128))
            self.assertEqual(preview.format, "JPEG")
        self.assertNotIn("thumbnail_path", photo)
        self.assertFalse((self.state / "thumbnails").exists())
        self.assertEqual(photo["content_hash"], hashlib.sha256(before[0]).hexdigest())
        with patch.object(ingest_module, "inspect_photo", side_effect=AssertionError("Unchanged photo decoded")):
            second = ingest(self.photos / ".", config=self.config)
        self.assertEqual(second["library_id"], first["library_id"])
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(second["changed_photo_ids"], [])
        self.assertEqual(before, (source.read_bytes(), source.stat().st_mtime_ns))
        self.assertEqual(thumb, self.preview(photo))

    def test_new_file_and_changed_content_keep_existing_identity(self):
        source = self.make_photo()
        first = ingest(self.photos, config=self.config)
        original = self.records(first["library_id"])[0]
        with SQLiteStorage(self.state) as store, store.transaction():
            store.put_photo(dict(original, needs_analysis=False))
        self.make_photo(color="orange")
        os.utime(source, ns=(source.stat().st_atime_ns, original["mtime_ns"] + 1_000_000_000))
        self.make_photo("second.png")
        second = ingest(self.photos, config=self.config)
        self.assertEqual((second["added"], second["updated"]), (1, 1))
        changed = {p["photo_id"]: p for p in self.records(first["library_id"])}[original["photo_id"]]
        self.assertNotEqual(changed["content_version"], original["content_version"])
        self.assertTrue(changed["needs_analysis"])
        self.assertIn(original["photo_id"], second["changed_photo_ids"])

    def test_timestamp_only_change_reuses_preview(self):
        source = self.make_photo()
        first = ingest(self.photos, config=self.config)
        original = self.records(first["library_id"])[0]
        stamp = original["mtime_ns"] + 1_000_000_000
        os.utime(source, ns=(source.stat().st_atime_ns, stamp))
        with patch.object(images_module, "_preview", side_effect=AssertionError("Identical bytes decoded")):
            second = ingest(self.photos, config=self.config)
        self.assertEqual((second["unchanged"], second["updated"]), (1, 0))
        self.assertEqual(self.records(first["library_id"])[0]["mtime_ns"], stamp)

    def test_missing_is_new_transition_and_restore_reuses_identity_and_analysis_flag(self):
        source = self.make_photo()
        contents = source.read_bytes()
        first = ingest(self.photos, config=self.config)
        original = self.records(first["library_id"])[0]
        with SQLiteStorage(self.state) as store, store.transaction():
            store.put_photo(dict(original, needs_analysis=False))
        source.unlink()
        missing = ingest(self.photos, config=self.config)
        again = ingest(self.photos, config=self.config)
        self.assertEqual((missing["missing"], again["missing"]), (1, 0))
        source.write_bytes(contents)
        restored = ingest(self.photos, config=self.config)
        self.assertEqual(restored["restored"], 1)
        self.assertEqual(restored["changed_photo_ids"], [])
        record = self.records(first["library_id"])[0]
        self.assertEqual(record["photo_id"], original["photo_id"])
        self.assertEqual(record["state"], "available")

    def test_missing_preview_and_changed_profile_are_repaired(self):
        self.make_photo()
        first = ingest(self.photos, config=self.config)
        original = self.records(first["library_id"])[0]
        with SQLiteStorage(self.state) as store:
            store.db.execute("DELETE FROM thumbnails WHERE photo_id=?", (original["photo_id"],))
        repair = ingest(self.photos, config=self.config)
        self.assertEqual(repair["updated"], 1)
        smaller = ingest(self.photos, config=Config(self.state, thumbnail_size=128))
        self.assertEqual(smaller["updated"], 1)
        record = self.records(first["library_id"])[0]
        self.assertEqual(record["metadata"]["thumbnail_width"], 128)

    def test_corrupt_file_does_not_stop_other_photos_and_existing_record_becomes_stale(self):
        source = self.make_photo()
        first = ingest(self.photos, config=self.config)
        original = self.records(first["library_id"])[0]
        source.write_bytes(b"broken image")
        self.make_photo("valid.png")
        second = ingest(self.photos, config=self.config)
        self.assertEqual((second["status"], second["added"], second["failed"]), ("partial", 1, 1))
        self.assertEqual(second["errors"][0]["photo_id"], original["photo_id"])
        stale = {p["photo_id"]: p for p in self.records(first["library_id"])}[original["photo_id"]]
        self.assertEqual(stale["state"], "error")
        self.assertEqual(stale["content_hash"], original["content_hash"])
        self.assertNotIn(original["photo_id"], second["changed_photo_ids"])

    def test_incomplete_directory_walk_never_marks_missing(self):
        first_path = self.make_photo("a.jpg")
        second_path = self.make_photo("b.jpg")
        first = ingest(self.photos, config=self.config)
        second_path.unlink()

        def interrupted_walk(*args):
            yield first_path
            raise PermissionError("Test unreadable subdirectory")

        with patch.object(ingest_module, "_walk", interrupted_walk):
            with self.assertRaises(PhotographyError) as failure:
                ingest(self.photos, config=self.config)
        self.assertEqual(failure.exception.code, "SCAN_INCOMPLETE")
        self.assertTrue(all(p["state"] == "available" for p in self.records(first["library_id"])))
        with SQLiteStorage(self.state) as store:
            record = store.scan(failure.exception.scan_id)
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["result"]["missing"], 0)

    def test_keyboard_interrupt_does_not_mark_missing(self):
        source = self.make_photo()
        first = ingest(self.photos, config=self.config)
        source.unlink()
        with patch.object(ingest_module, "_walk", side_effect=KeyboardInterrupt):
            with self.assertRaises(PhotographyError) as failure:
                ingest(self.photos, config=self.config)
        self.assertEqual(failure.exception.code, "SCAN_INTERRUPTED")
        self.assertEqual(self.records(first["library_id"])[0]["state"], "available")

    def test_mid_read_modification_is_not_saved_as_valid(self):
        source = self.make_photo()
        real_preview = images_module._preview

        def changing_preview(handle, config):
            result = real_preview(handle, config)
            info = source.stat()
            os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            return result

        with patch.object(images_module, "_preview", changing_preview):
            result = ingest(self.photos, config=self.config)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["errors"][0]["code"], "FILE_CHANGED_DURING_SCAN")
        self.assertEqual(self.records(result["library_id"]), [])

    def test_mid_photo_interrupt_preserves_consistent_failed_scan_counts(self):
        self.make_photo()
        with patch.object(ingest_module, "inspect_photo", side_effect=KeyboardInterrupt):
            with self.assertRaises(PhotographyError) as failure:
                ingest(self.photos, config=self.config)
        with SQLiteStorage(self.state) as store:
            result = store.scan(failure.exception.scan_id)["result"]
        self.assertEqual((result["scanned"], result["failed"], result["missing"]), (1, 1, 0))

    def test_narrowing_format_scope_does_not_mark_excluded_photos_missing(self):
        self.make_photo()
        first = ingest(self.photos, config=self.config)
        result = ingest(self.photos, config=Config(self.state, extensions=(".png",)))
        self.assertEqual(result["missing"], 0)
        self.assertEqual(self.records(first["library_id"])[0]["state"], "available")

    def test_exif_orientation_and_metadata(self):
        exif = Image.Exif()
        exif[274], exif[271], exif[272] = 6, "Test camera", "Synthetic fixture"
        self.make_photo(exif=exif)
        first = ingest(self.photos, config=self.config)
        record = self.records(first["library_id"])[0]
        metadata = record["metadata"]
        self.assertEqual((metadata["display_width"], metadata["display_height"]), (320, 640))
        self.assertEqual(metadata["exif"]["make"], "Test camera")
        with Image.open(io.BytesIO(self.preview(record)["data"])) as preview:
            self.assertEqual(preview.size, (128, 256))
            self.assertNotIn(274, preview.getexif())

    def test_supported_formats_alpha_small_images_and_color_profile(self):
        for extension in ("jpg", "png", "webp", "tiff", "bmp"):
            self.make_photo(f"image.{extension}", size=(80, 40))
        Image.new("RGBA", (20, 20), (0, 0, 0, 0)).save(self.photos / "transparent.png")
        self.make_photo("icc.jpg", icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
        result = ingest(self.photos, config=self.config)
        self.assertEqual((result["added"], result["failed"]), (7, 0))
        by_name = {p["relative_path"]: p for p in self.records(result["library_id"])}
        self.assertEqual(by_name["image.jpg"]["metadata"]["thumbnail_width"], 80)
        self.assertEqual(by_name["icc.jpg"]["metadata"]["color_handling"], "converted_to_srgb")
        with Image.open(io.BytesIO(self.preview(by_name["transparent.png"])["data"])) as preview:
            self.assertGreater(min(preview.getpixel((5, 5))), 245)

    def test_multiple_libraries_and_identical_files_keep_separate_records(self):
        source = self.make_photo()
        (self.photos / "copy.jpg").write_bytes(source.read_bytes())
        first = ingest(self.photos, config=self.config)
        other = self.base / "other"
        other.mkdir()
        (other / "image.jpg").write_bytes(source.read_bytes())
        second = ingest(other, config=self.config)
        self.assertNotEqual(first["library_id"], second["library_id"])
        all_photos = self.records(first["library_id"]) + self.records(second["library_id"])
        self.assertEqual(len({p["photo_id"] for p in all_photos}), 3)
        self.assertEqual(len({p["content_hash"] for p in all_photos}), 1)

    def test_invalid_and_overlapping_paths_do_not_write_state(self):
        for invalid in ("relative-path", self.base / "not-found", self.photos / "state"):
            with self.assertRaises(PhotographyError):
                ingest(invalid, config=self.config)
        self.assertFalse(self.state.exists())
        with self.assertRaises(PhotographyError) as failure:
            ingest(self.photos, config=Config(self.photos / "state"))
        self.assertEqual(failure.exception.code, "STATE_OVERLAPS_LIBRARY")
        self.assertFalse((self.photos / "state").exists())

    def test_root_permission_failure_does_not_initialize_database(self):
        with patch.object(ingest_module.os, "scandir", side_effect=PermissionError("Test denied")):
            with self.assertRaises(PhotographyError) as failure:
                ingest(self.photos, config=self.config)
        self.assertEqual(failure.exception.code, "DIRECTORY_UNREADABLE")
        self.assertFalse(self.state.exists())

    def test_empty_library_unsupported_and_multipage_files(self):
        (self.photos / "ignored.raw").write_bytes(b"not supported")
        empty = ingest(self.photos, config=self.config)
        self.assertEqual(empty["scanned"], 0)
        Image.new("RGB", (20, 20)).save(self.photos / "pages.tiff", save_all=True,
                                      append_images=[Image.new("RGB", (20, 20), "red")])
        result = ingest(self.photos, config=self.config)
        self.assertEqual(result["errors"][0]["code"], "MULTIFRAME_UNSUPPORTED")

    def test_paginated_queries_and_persisted_scan_events(self):
        for number in range(3):
            self.make_photo(f"{number}.jpg")
        first = ingest(self.photos, config=self.config)
        second = ingest(self.photos, config=self.config)
        with SQLiteStorage(self.state) as store:
            page1 = store.photos(first["library_id"], limit=2)
            page2 = store.photos(first["library_id"], limit=2, after=page1["next_cursor"])
            self.assertEqual(len(page1["items"] + page2["items"]), 3)
            self.assertIsNone(page2["next_cursor"])
            events = store.events(first["scan_id"], limit=2)
            tail = store.events(first["scan_id"], limit=2, after=events["next_cursor"])
            self.assertEqual(len(events["items"] + tail["items"]), 3)
            self.assertEqual(store.events(second["scan_id"], changes_only=True)["items"], [])
            self.assertEqual(store.scan(first["scan_id"])["result"], first)
            self.assertEqual(store.libraries()[0]["available"], 3)

    def test_cli_is_runnable_from_an_unrelated_working_directory(self):
        self.make_photo()
        script = PROJECT / "photography" / "scripts" / "photography.py"
        args = [sys.executable, str(script), "--state-dir", str(self.state)]
        first = subprocess.run(args + ["ingest", str(self.photos)], cwd=self.base,
                               capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["added"], 1)
        second = subprocess.run(args + ["ingest", str(self.photos)], cwd=self.base,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(json.loads(second.stdout)["unchanged"], 1)
        failure = subprocess.run(args + ["ingest", "relative"], cwd=self.base,
                                 capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(failure.returncode, 2)
        self.assertEqual(json.loads(failure.stdout)["error"]["code"], "INVALID_PATH")


if __name__ == "__main__":
    unittest.main()
