from __future__ import annotations

import json
import hashlib
import unicodedata
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .config import PhotographyError
from .thumbnails import MAX_PREVIEW_BYTES, validate_preview
from .workflow_storage import WORKFLOW_SCHEMA, WorkflowStorage


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS libraries (
        library_id TEXT PRIMARY KEY, root_path TEXT NOT NULL,
        root_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS photos (
        photo_id TEXT PRIMARY KEY,
        library_id TEXT NOT NULL REFERENCES libraries(library_id),
        path_key TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('available','missing','error')),
        content_hash TEXT NOT NULL, data_json TEXT NOT NULL,
        UNIQUE(library_id, path_key))""",
    "CREATE INDEX IF NOT EXISTS photos_library ON photos(library_id, photo_id)",
    "CREATE INDEX IF NOT EXISTS photos_hash ON photos(content_hash)",
    """CREATE TABLE IF NOT EXISTS scans (
        scan_id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES libraries(library_id),
        started_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL,
        result_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS scan_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id TEXT NOT NULL REFERENCES scans(scan_id),
        kind TEXT NOT NULL, photo_id TEXT, path TEXT NOT NULL, error_json TEXT)""",
    "CREATE INDEX IF NOT EXISTS events_scan ON scan_events(scan_id, event_id)",
    """CREATE TABLE IF NOT EXISTS analyses (
        analysis_id TEXT PRIMARY KEY,
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        cache_key TEXT NOT NULL, created_at TEXT NOT NULL, data_json TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS analyses_cache ON analyses(photo_id,cache_key,created_at)",
    """CREATE TABLE IF NOT EXISTS analysis_runs (
        run_id TEXT PRIMARY KEY, status TEXT NOT NULL, data_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS albums (
        album_id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS album_photos (
        album_id TEXT NOT NULL REFERENCES albums(album_id),
        photo_id TEXT NOT NULL REFERENCES photos(photo_id), added_at TEXT NOT NULL,
        PRIMARY KEY(album_id,photo_id))""",
    "CREATE INDEX IF NOT EXISTS album_photos_photo ON album_photos(photo_id,album_id)",
    """CREATE TABLE IF NOT EXISTS thumbnails (
        photo_id TEXT PRIMARY KEY REFERENCES photos(photo_id),
        content_version TEXT NOT NULL, profile TEXT NOT NULL, image_hash TEXT NOT NULL,
        mime_type TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
        created_at TEXT NOT NULL, size_bytes INTEGER NOT NULL, data BLOB NOT NULL)""",
    # Preserve the v4/v5 archive schema; retired text-index rows are never rewritten.
    """CREATE TABLE IF NOT EXISTS embedding_encoders (
        encoder_id TEXT PRIMARY KEY, profile_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS photo_embeddings (
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        encoder_id TEXT NOT NULL REFERENCES embedding_encoders(encoder_id),
        analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id),
        content_version TEXT NOT NULL, text_hash TEXT NOT NULL, recipe_version TEXT NOT NULL,
        dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
        dtype TEXT NOT NULL CHECK(dtype='float32-le'), normalized INTEGER NOT NULL CHECK(normalized=1),
        vector BLOB NOT NULL, vector_hash TEXT NOT NULL, token_count INTEGER NOT NULL,
        truncated INTEGER NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(photo_id,encoder_id))""",
    "CREATE INDEX IF NOT EXISTS embeddings_encoder ON photo_embeddings(encoder_id,photo_id)",
)


class SQLiteStorage(WorkflowStorage):
    def __init__(self, state_dir: Path):
        self.db = None
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(state_dir / "photography.db", timeout=5, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version in (1, 2, 3, 4):
                # Keep a consistent pre-migration snapshot, including analysis history.
                backup_dir = state_dir / "backups"
                backup_dir.mkdir(exist_ok=True)
                with closing(sqlite3.connect(backup_dir / f"schema-v{version}-{uuid4().hex}.db")) as backup:
                    self.db.backup(backup)
            with self.transaction():
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2, 3, 4, 5):
                    raise PhotographyError("SCHEMA_UNSUPPORTED", f"Unsupported database schema: {version}.")
                for statement in SCHEMA:
                    self.db.execute(statement)
                for statement in WORKFLOW_SCHEMA:
                    self.db.execute(statement)
                if version in (1, 2):
                    self._migrate_thumbnails(state_dir)
                self.db.execute("PRAGMA user_version=5")
        except (OSError, sqlite3.Error) as exc:
            self.close()
            raise PhotographyError("STORAGE_UNAVAILABLE", str(exc)) from exc
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def _migrate_thumbnails(self, state_dir):
        failures = []
        for row in self.db.execute("SELECT data_json FROM photos").fetchall():
            photo = json.loads(row[0])
            try:
                if "thumbnail_path" in photo:
                    path = (state_dir / photo["thumbnail_path"]).resolve()
                    if not path.is_relative_to(state_dir.resolve()):
                        raise PhotographyError("INVALID_PREVIEW", "Legacy preview path is outside the state directory.")
                    with path.open("rb") as handle:
                        data = handle.read(MAX_PREVIEW_BYTES + 1)
                    self.put_thumbnail(photo, data)
                else:
                    # Allows existing v3 fixtures to exercise old upgrade paths.
                    from .thumbnails import stored_preview
                    stored_preview(photo, self)
                photo.pop("thumbnail_path", None)
                photo["thumbnail_id"] = photo["photo_id"]
                self.put_photo(photo)
            except (OSError, PhotographyError, KeyError) as exc:
                failures.append({"photo_id": photo.get("photo_id"),
                                 "path": photo.get("thumbnail_path"), "message": str(exc)})
        if failures:
            raise PhotographyError("THUMBNAIL_MIGRATION_FAILED",
                "Migration was rolled back. Restore the listed previews using the prior version, then retry.",
                details=failures)

    @contextmanager
    def savepoint(self):
        key = "sp_" + uuid4().hex
        self.db.execute(f"SAVEPOINT {key}")
        try:
            yield
            self.db.execute(f"RELEASE SAVEPOINT {key}")
        except BaseException:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {key}")
            self.db.execute(f"RELEASE SAVEPOINT {key}")
            raise

    def put_thumbnail(self, photo, data):
        width, height = validate_preview(data)
        self.db.execute("""INSERT INTO thumbnails VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(photo_id) DO UPDATE SET content_version=excluded.content_version,
            profile=excluded.profile,image_hash=excluded.image_hash,mime_type=excluded.mime_type,
            width=excluded.width,height=excluded.height,created_at=excluded.created_at,size_bytes=excluded.size_bytes,data=excluded.data""",
            (photo["photo_id"], photo["content_version"], photo["thumbnail_profile"],
             hashlib.sha256(data).hexdigest(), "image/jpeg", width, height, now(), len(data), data))

    def thumbnail(self, photo_id, *, include_data=True):
        fields = "*" if include_data else "photo_id,content_version,profile,image_hash,mime_type,width,height,created_at,size_bytes"
        row = self.db.execute(f"SELECT {fields} FROM thumbnails WHERE photo_id=?", (photo_id,)).fetchone()
        if row is None:
            raise PhotographyError("INVALID_PREVIEW", "Stored preview does not exist; rescan to repair it.")
        return dict(row)

    @staticmethod
    def album_name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
            raise PhotographyError("INVALID_ARGUMENT", "Album name must contain 1 to 200 characters.")
        name = unicodedata.normalize("NFC", name.strip())
        return name, name.casefold()

    def create_album(self, name):
        name, key = self.album_name(name)
        with self.savepoint():
            stamp = now()
            new_id = "album_" + uuid4().hex
            created = self.db.execute("INSERT INTO albums VALUES (?,?,?,?,?) ON CONFLICT(name_key) DO NOTHING",
                                     (new_id, name, key, stamp, stamp)).rowcount == 1
            item = dict(self.db.execute("SELECT * FROM albums WHERE name_key=?", (key,)).fetchone())
        return {**item, "created": created}

    def album(self, album_id):
        row = self.db.execute("SELECT * FROM albums WHERE album_id=?", (album_id,)).fetchone()
        if row is None:
            raise PhotographyError("ALBUM_NOT_FOUND", "Album does not exist.")
        return dict(row)

    def albums(self):
        return [dict(row) for row in self.db.execute("""SELECT a.*, COUNT(ap.photo_id) AS photo_count
            FROM albums a LEFT JOIN album_photos ap ON ap.album_id=a.album_id
            GROUP BY a.album_id ORDER BY a.name_key,a.album_id""")]

    def rename_album(self, album_id, name):
        name, key = self.album_name(name)
        with self.savepoint():
            self.album(album_id)
            try:
                self.db.execute("UPDATE albums SET name=?,name_key=?,updated_at=? WHERE album_id=?",
                                (name, key, now(), album_id))
            except sqlite3.IntegrityError:
                raise PhotographyError("ALBUM_NAME_EXISTS", "Another album already uses this name.") from None
        return self.album(album_id)

    def change_members(self, album_id, photo_ids, *, remove=False):
        if not photo_ids or any(not isinstance(p, str) or not p for p in photo_ids):
            raise PhotographyError("INVALID_ARGUMENT", "Provide at least one photo ID.")
        ids = list(dict.fromkeys(photo_ids))
        changed = 0
        with self.savepoint():
            self.album(album_id)
            for photo_id in ids:
                self.photo(photo_id)
                if remove:
                    changed += self.db.execute("DELETE FROM album_photos WHERE album_id=? AND photo_id=?",
                                               (album_id, photo_id)).rowcount
                else:
                    changed += self.db.execute("INSERT INTO album_photos VALUES (?,?,?) ON CONFLICT DO NOTHING",
                                               (album_id, photo_id, now())).rowcount
            if changed:
                self.db.execute("UPDATE albums SET updated_at=? WHERE album_id=?", (now(), album_id))
        return {"album_id": album_id, "requested": len(ids),
                "removed" if remove else "added": changed, "unchanged": len(ids) - changed}

    def album_photos(self, album_id, limit=100, after=""):
        self._limit(limit)
        self.album(album_id)
        rows = self.db.execute("""SELECT p.data_json FROM photos p JOIN album_photos ap ON p.photo_id=ap.photo_id
            WHERE ap.album_id=? AND p.photo_id>? ORDER BY p.photo_id LIMIT ?""",
            (album_id, after, limit + 1)).fetchall()
        items = [json.loads(row[0]) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["photo_id"] if len(rows) > limit else None}

    def photos_for_album(self, album_id):
        self.album(album_id)
        return [json.loads(row[0]) for row in self.db.execute("""SELECT p.data_json FROM photos p
            JOIN album_photos ap ON ap.photo_id=p.photo_id WHERE ap.album_id=? ORDER BY p.photo_id""", (album_id,))]

    def analysis_records(self, photo_id):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT data_json FROM analyses WHERE photo_id=? ORDER BY created_at DESC,rowid DESC", (photo_id,))]

    def latest_analysis_failure(self, photo_id):
        # Runs retain their existing JSON format; this query reads only matching failures.
        row = self.db.execute("""SELECT r.data_json,e.value FROM analysis_runs r, json_each(r.data_json,'$.results') e
            WHERE json_extract(e.value,'$.photo_id')=? ORDER BY r.rowid DESC LIMIT 1""", (photo_id,)).fetchone()
        if row:
            entry = json.loads(row[1])
            if entry["status"] == "failed":
                return {"run_id": json.loads(row[0])["run_id"], "error": entry.get("error")}
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def read_snapshot(self):
        self.db.execute("BEGIN")
        try:
            yield
        finally:
            self.db.execute("ROLLBACK")

    @contextmanager
    def transaction(self):
        # Serialize writers throughout a scan; readers still see the prior snapshot.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def library(self, root_path: str, root_key: str) -> dict:
        record = self.db.execute("SELECT * FROM libraries WHERE root_key=?", (root_key,)).fetchone()
        if record:
            return dict(record)
        item = dict(library_id=f"lib_{uuid4().hex}", root_path=root_path, root_key=root_key, created_at=now())
        self.db.execute("INSERT INTO libraries VALUES (:library_id,:root_path,:root_key,:created_at)", item)
        return item

    def photos_for_library(self, library_id: str) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT data_json FROM photos WHERE library_id=?", (library_id,))]

    def put_photo(self, photo: dict):
        self.db.execute("""INSERT INTO photos VALUES (?,?,?,?,?,?)
            ON CONFLICT(photo_id) DO UPDATE SET state=excluded.state,
                content_hash=excluded.content_hash, data_json=excluded.data_json""",
            (photo["photo_id"], photo["library_id"], photo["path_key"], photo["state"],
             photo["content_hash"], json.dumps(photo, ensure_ascii=False)))

    def start_scan(self, scan_id: str, library_id: str):
        self.db.execute("INSERT INTO scans(scan_id,library_id,started_at,status) VALUES (?,?,?,'running')",
                        (scan_id, library_id, now()))

    def event(self, scan_id: str, kind: str, photo_id: str | None, path: str, error: dict | None = None):
        self.db.execute("INSERT INTO scan_events(scan_id,kind,photo_id,path,error_json) VALUES (?,?,?,?,?)",
                        (scan_id, kind, photo_id, path, json.dumps(error) if error else None))

    def finish_scan(self, scan_id: str, result: dict):
        self.db.execute("UPDATE scans SET completed_at=?,status=?,result_json=? WHERE scan_id=?",
                        (now(), result["status"], json.dumps(result, ensure_ascii=False), scan_id))

    def libraries(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("""SELECT l.*,
            COUNT(p.photo_id) AS total_records,
            SUM(CASE WHEN p.state='available' THEN 1 ELSE 0 END) AS available,
            SUM(CASE WHEN p.state='missing' THEN 1 ELSE 0 END) AS missing,
            SUM(CASE WHEN p.state='error' THEN 1 ELSE 0 END) AS errors
            FROM libraries l LEFT JOIN photos p ON p.library_id=l.library_id
            GROUP BY l.library_id ORDER BY l.created_at,l.library_id""")]

    @staticmethod
    def _limit(limit: int):
        if not 1 <= limit <= 1000:
            raise PhotographyError("INVALID_ARGUMENT", "Page limit must be between 1 and 1000.")

    def photos(self, library_id: str, limit: int = 100, after: str = "") -> dict:
        self._limit(limit)
        if not self.db.execute("SELECT 1 FROM libraries WHERE library_id=?", (library_id,)).fetchone():
            raise PhotographyError("LIBRARY_NOT_FOUND", "Photo library does not exist.")
        rows = self.db.execute("""SELECT data_json FROM photos
            WHERE library_id=? AND photo_id>? ORDER BY photo_id LIMIT ?""",
            (library_id, after, limit + 1)).fetchall()
        items = [json.loads(row[0]) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["photo_id"] if len(rows) > limit else None}

    def photo(self, photo_id: str) -> dict:
        row = self.db.execute("SELECT data_json FROM photos WHERE photo_id=?", (photo_id,)).fetchone()
        if row is None:
            raise PhotographyError("PHOTO_NOT_FOUND", "Photo does not exist.")
        return json.loads(row[0])

    def scan(self, scan_id: str) -> dict:
        row = self.db.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
        if row is None:
            raise PhotographyError("SCAN_NOT_FOUND", "Scan does not exist.")
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json")) if row["result_json"] else None
        return result

    def events(self, scan_id: str, limit: int = 100, after: int = 0, changes_only: bool = False) -> dict:
        self._limit(limit)
        self.scan(scan_id)
        clause = " AND kind<>'unchanged'" if changes_only else ""
        rows = self.db.execute(f"""SELECT * FROM scan_events WHERE scan_id=? AND event_id>?
            {clause} ORDER BY event_id LIMIT ?""", (scan_id, after, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            item["error"] = json.loads(item.pop("error_json")) if row["error_json"] else None
            items.append(item)
        return {"items": items, "next_cursor": items[-1]["event_id"] if len(rows) > limit else None}

    def cached_analysis(self, photo_id: str, cache_key: str) -> dict | None:
        row = self.db.execute("""SELECT data_json FROM analyses
            WHERE photo_id=? AND cache_key=? ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (photo_id, cache_key)).fetchone()
        return json.loads(row[0]) if row else None

    def put_analysis(self, record: dict):
        self.db.execute("INSERT INTO analyses VALUES (?,?,?,?,?)",
                        (record["analysis_id"], record["photo_id"], record["cache_key"],
                         record["created_at"], json.dumps(record, ensure_ascii=False)))

    def analysis(self, analysis_id: str) -> dict:
        row = self.db.execute("SELECT data_json FROM analyses WHERE analysis_id=?", (analysis_id,)).fetchone()
        if row is None:
            raise PhotographyError("ANALYSIS_NOT_FOUND", "Analysis does not exist.")
        return json.loads(row[0])

    def analyses(self, photo_id: str, limit: int = 100, after: int = 0) -> dict:
        self._limit(limit)
        photo = self.photo(photo_id)
        rows = self.db.execute("""SELECT rowid,data_json FROM analyses
            WHERE photo_id=? AND rowid>? ORDER BY rowid LIMIT ?""", (photo_id, after, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            item = json.loads(row[1])
            item["matches_indexed_photo"] = (photo["state"] == "available"
                and item["content_version"] == photo["content_version"]
                and item["thumbnail_profile"] == photo["thumbnail_profile"])
            items.append(item)
        return {"items": items, "next_cursor": rows[limit - 1][0] if len(rows) > limit else None}

    def save_analysis_run(self, result: dict):
        self.db.execute("""INSERT INTO analysis_runs VALUES (?,?,?)
            ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,data_json=excluded.data_json""",
            (result["run_id"], result["status"], json.dumps(result, ensure_ascii=False)))

    def analysis_run(self, run_id: str) -> dict:
        row = self.db.execute("SELECT data_json FROM analysis_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise PhotographyError("ANALYSIS_RUN_NOT_FOUND", "Analysis run does not exist.")
        return json.loads(row[0])
