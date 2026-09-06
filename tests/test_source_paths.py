from pathlib import Path
import os
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
from photography_lib import source_paths
from photography_lib.sqlite_storage import SQLiteStorage


class SourcePathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="portable-paths-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.original = self.root / "original.jpg"
        Image.new("RGB", (90, 60), "navy").save(self.original)
        self.database = self.root / "album.sqlite"
        SQLiteStorage.create(self.database).close()
        ingestion(self.root, config=Config(self.database, model_cache_root=self.root / "cache"))
        self.store = SQLiteStorage.open(self.database, writable=True)
        self.addCleanup(self.store.close)
        self.photo = self.store.photos()[0]

    def test_absolute_is_preferred_even_when_relative_has_different_contents(self):
        relative = self.root / "different.jpg"
        Image.new("RGB", (20, 40), "red").save(relative)
        photo = {**self.photo, "original_relative_path": relative.name}
        result = source_paths.resolve_original(photo, self.database)
        self.assertEqual((result["path"], result["via"], result["repair_required"]), (str(self.original), "absolute", False))
        self.assertFalse(result["content_verified"])
        self.assertEqual(source_paths.photo_filename(photo), self.original.name)

    def test_relative_fallback_persists_absolute_without_changing_content(self):
        relative = self.root / "relocated.jpg"
        self.original.rename(relative)
        self.store.put_photo({**self.photo, "original_relative_path": relative.name})
        before = self.store.photo(self.photo["photo_id"])
        result = source_paths.resolve_original(before, self.database)
        self.assertEqual((result["status"], result["via"], result["repair_required"]), ("available", "relative", True))
        updated = source_paths.persist_resolution(self.store, before, result)
        self.assertEqual(updated["original_absolute_path"], str(relative))
        for key in ("photo_id", "content_version", "thumbnail_profile", "metadata", "updated_at"):
            self.assertEqual(updated[key], before[key])
        self.assertEqual(updated["original_relative_path"], relative.name)

    def test_both_missing_or_no_relative_marks_missing(self):
        self.original.unlink()
        for relative in ("missing.jpg", None):
            result = source_paths.resolve_original({**self.photo, "original_relative_path": relative}, self.database)
            self.assertEqual(result["status"], "missing")
            self.assertFalse(result["repair_required"])

    def test_permission_error_is_not_missing_and_does_not_fall_back(self):
        copy = self.root / "copy.jpg"
        shutil.copyfile(self.original, copy)
        photo = {**self.photo, "original_relative_path": copy.name}
        real_open = Path.open
        def denied(path, *args, **kwargs):
            if path == self.original:
                raise PermissionError("Synthetic permission failure")
            return real_open(path, *args, **kwargs)
        with patch.object(Path, "open", denied):
            result = source_paths.resolve_original(photo, self.database)
        self.assertEqual((result["status"], result["via"]), ("unavailable", "absolute"))
        self.assertEqual(result["error"]["code"], "ORIGINAL_UNAVAILABLE")

    def test_relative_base_is_database_parent_not_cwd_and_allows_parent_segments(self):
        directory = self.root / "nested"
        directory.mkdir()
        database = directory / "other.sqlite"
        photo = {**self.photo, "original_absolute_path": str(self.root / "absent.jpg"),
                 "original_relative_path": "../original.jpg"}
        old_cwd = Path.cwd()
        try:
            os.chdir(directory)
            result = source_paths.resolve_original(photo, database)
        finally:
            os.chdir(old_cwd)
        self.assertEqual(result["path"], str(self.original))

    def test_foreign_absolute_path_is_not_interpreted_from_cwd(self):
        foreign = "/unavailable-host/photos/photo.jpg" if os.name == "nt" else r"Z:\unavailable-host\photo.jpg"
        result = source_paths.resolve_original({**self.photo, "original_absolute_path": foreign}, self.database)
        self.assertEqual((result["path"], result["via"]), (str(self.original), "relative"))

    def test_cross_drive_relative_path_is_explicitly_unavailable(self):
        with patch.object(source_paths.os.path, "relpath", side_effect=ValueError("different mount")):
            relative, warning = source_paths.relative_original_path(self.original, self.database)
        self.assertIsNone(relative)
        self.assertEqual(warning["code"], "RELATIVE_PATH_UNAVAILABLE")

    def test_readonly_repair_fails_without_mutation(self):
        other = self.root / "moved.jpg"
        self.original.rename(other)
        photo = {**self.photo, "original_relative_path": other.name}
        self.store.put_photo(photo)
        result = source_paths.resolve_original(photo, self.database)
        with SQLiteStorage.open(self.database) as readonly, self.assertRaises(PhotographyError):
            source_paths.persist_resolution(readonly, photo, result)
        self.assertEqual(self.store.photo(photo["photo_id"])["original_absolute_path"], str(self.original))

    def test_concurrent_update_blocks_stale_path_repair(self):
        result = source_paths.resolve_original(self.photo, self.database)
        self.store.put_photo({**self.photo, "original_relative_path": None})
        with self.assertRaises(PhotographyError) as error:
            source_paths.persist_resolution(self.store, self.photo, result)
        self.assertEqual(error.exception.code, "PHOTO_PATH_CHANGED")

    def test_relink_validates_hash_and_rejects_different_photo(self):
        new = self.root / "same.jpg"
        shutil.copyfile(self.original, new)
        result = source_paths.relink_original(self.photo, new, self.database)
        self.assertTrue(result["content_verified"])
        updated = source_paths.persist_resolution(self.store, self.photo, result)
        self.assertEqual(updated["photo_id"], self.photo["photo_id"])
        self.assertEqual(updated["original_relative_path"], "same.jpg")
        new.write_bytes(b"different contents")
        with self.assertRaises(PhotographyError) as error:
            source_paths.relink_original(updated, new, self.database)
        self.assertEqual(error.exception.code, "ORIGINAL_CONTENT_MISMATCH")

    def test_absolute_relative_path_or_directory_is_not_a_valid_fallback(self):
        missing = {**self.photo, "original_absolute_path": str(self.root / "missing.jpg")}
        result = source_paths.resolve_original({**missing, "original_relative_path": str(self.original)}, self.database)
        self.assertEqual(result["status"], "unavailable")
        result = source_paths.resolve_original({**self.photo, "original_absolute_path": str(self.root)}, self.database)
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
