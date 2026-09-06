"""Static, album-local virtual folders and many-to-many photo membership."""
from __future__ import annotations

from datetime import datetime, timezone

from .config import PhotographyError


VIRTUAL_FOLDER_TABLES = ("virtual_folders", "virtual_folder_photos")

VIRTUAL_FOLDER_SCHEMA = (
    """CREATE TABLE virtual_folders (
        folder_id TEXT PRIMARY KEY NOT NULL,
        name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE virtual_folder_photos (
        folder_id TEXT NOT NULL REFERENCES virtual_folders(folder_id) ON DELETE CASCADE,
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        added_at TEXT NOT NULL,
        PRIMARY KEY(folder_id,photo_id))""",
    "CREATE INDEX virtual_folder_photos_photo ON virtual_folder_photos(photo_id,folder_id)",
)

_FOLDERS = """SELECT f.*,
    (SELECT COUNT(*) FROM virtual_folder_photos m WHERE m.folder_id=f.folder_id) AS photo_count
    FROM virtual_folders f"""


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


class VirtualFolderStorage:
    def folder(self, folder_id):
        row = self.db.execute(_FOLDERS + " WHERE f.folder_id=?", (folder_id,)).fetchone()
        if row is None:
            raise PhotographyError("FOLDER_NOT_FOUND", "Virtual folder does not exist in this album.")
        return dict(row)

    def folders(self):
        return [dict(row) for row in self.db.execute(_FOLDERS + " ORDER BY f.folder_id")]

    def folder_by_name_key(self, key):
        row = self.db.execute(_FOLDERS + " WHERE f.name_key=?", (key,)).fetchone()
        return dict(row) if row is not None else None

    def photo_folders(self, photo_id):
        self.photo(photo_id)
        return [dict(row) for row in self.db.execute(_FOLDERS + """
            WHERE EXISTS (SELECT 1 FROM virtual_folder_photos m
                WHERE m.folder_id=f.folder_id AND m.photo_id=?) ORDER BY f.folder_id""", (photo_id,))]

    def photos_in_folders(self, folder_ids, match):
        if match not in ("union", "intersection"):
            raise PhotographyError("INVALID_ARGUMENT", "Folder match must be union or intersection.")
        ids = sorted(set(folder_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        clause = " HAVING COUNT(*)=?" if match == "intersection" else ""
        parameters = (*ids, len(ids)) if match == "intersection" else ids
        return [self._photo(row) for row in self.db.execute(f"""
            SELECT p.* FROM photos p WHERE p.photo_id IN (
                SELECT photo_id FROM virtual_folder_photos
                WHERE folder_id IN ({placeholders}) GROUP BY photo_id{clause})
            ORDER BY p.photo_id""", parameters)]

    def _create_folder(self, folder):
        self.assert_writable()
        self.db.execute("""INSERT INTO virtual_folders
            (folder_id,name,name_key,description,created_at,updated_at) VALUES (?,?,?,?,?,?)""",
            tuple(folder[key] for key in (
                "folder_id", "name", "name_key", "description", "created_at", "updated_at")))

    def _rename_folder(self, folder_id, name, name_key):
        self.assert_writable()
        self.db.execute("UPDATE virtual_folders SET name=?,name_key=?,updated_at=? WHERE folder_id=?",
                        (name, name_key, _timestamp(), folder_id))

    def _delete_folder(self, folder_id):
        self.assert_writable()
        self.db.execute("DELETE FROM virtual_folders WHERE folder_id=?", (folder_id,))

    def _add_folder_photos(self, folder_id, photo_ids):
        with self.savepoint():
            timestamp = _timestamp()
            cursor = self.db.executemany("""INSERT INTO virtual_folder_photos VALUES (?,?,?)
                ON CONFLICT(folder_id,photo_id) DO NOTHING""",
                ((folder_id, photo_id, timestamp) for photo_id in photo_ids))
            changed = cursor.rowcount
            if changed:
                self.db.execute("UPDATE virtual_folders SET updated_at=? WHERE folder_id=?",
                                (timestamp, folder_id))
        return changed

    def _remove_folder_photos(self, folder_id, photo_ids):
        with self.savepoint():
            cursor = self.db.executemany(
                "DELETE FROM virtual_folder_photos WHERE folder_id=? AND photo_id=?",
                ((folder_id, photo_id) for photo_id in photo_ids))
            changed = cursor.rowcount
            if changed:
                self.db.execute("UPDATE virtual_folders SET updated_at=? WHERE folder_id=?",
                                (_timestamp(), folder_id))
        return changed
