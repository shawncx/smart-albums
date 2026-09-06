from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from .config import Config, PhotographyError
from .image_embedding_storage import IMAGE_EMBEDDING_SCHEMA, IMAGE_EMBEDDING_TABLES, ImageEmbeddingStorage
from .thumbnails import validate_preview
from .virtual_folder_storage import VIRTUAL_FOLDER_SCHEMA, VIRTUAL_FOLDER_TABLES, VirtualFolderStorage


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


APPLICATION_ID = 0x53414C42
SCHEMA_VERSION = 9

SCHEMA = (
    """CREATE TABLE album_metadata (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        album_uuid TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE photos (
        photo_id TEXT PRIMARY KEY NOT NULL,
        original_absolute_path TEXT NOT NULL, original_relative_path TEXT,
        content_version TEXT NOT NULL, thumbnail_profile TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK(size_bytes>=0), mtime_ns INTEGER NOT NULL,
        metadata_json TEXT NOT NULL,
        ingest_state TEXT NOT NULL CHECK(ingest_state IN ('available','error')),
        original_status TEXT NOT NULL CHECK(original_status IN ('not_checked','available','missing','unavailable')),
        last_ingest_error TEXT, last_path_error TEXT, last_original_check TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, path_updated_at TEXT)""",
    "CREATE INDEX photos_content_version ON photos(content_version)",
    "CREATE INDEX photos_original_absolute_path ON photos(original_absolute_path)",
    "CREATE INDEX photos_original_relative_path ON photos(original_relative_path)",
    """CREATE TABLE thumbnails (
        photo_id TEXT PRIMARY KEY NOT NULL REFERENCES photos(photo_id),
        content_version TEXT NOT NULL, profile TEXT NOT NULL, image_hash TEXT NOT NULL,
        mime_type TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
        created_at TEXT NOT NULL, size_bytes INTEGER NOT NULL, data BLOB NOT NULL)""",
    """CREATE TABLE scans (
        scan_id TEXT PRIMARY KEY NOT NULL,
        source_absolute_path TEXT NOT NULL, source_relative_path TEXT,
        started_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL, result_json TEXT)""",
    """CREATE TABLE scan_events (
        event_id INTEGER PRIMARY KEY,
        scan_id TEXT NOT NULL REFERENCES scans(scan_id),
        kind TEXT NOT NULL, photo_id TEXT REFERENCES photos(photo_id),
        path TEXT NOT NULL, error_json TEXT)""",
    "CREATE INDEX events_scan ON scan_events(scan_id,event_id)",
)

REQUIRED_COLUMNS = {
    "album_metadata": {"singleton", "album_uuid", "created_at"},
    "photos": {
        "photo_id", "original_absolute_path", "original_relative_path", "content_version",
        "thumbnail_profile", "size_bytes", "mtime_ns", "metadata_json", "ingest_state",
        "original_status", "last_ingest_error", "last_path_error", "last_original_check",
        "created_at", "updated_at", "path_updated_at",
    },
    "thumbnails": {
        "photo_id", "content_version", "profile", "image_hash", "mime_type", "width", "height",
        "created_at", "size_bytes", "data",
    },
    "scans": {
        "scan_id", "source_absolute_path", "source_relative_path", "started_at",
        "completed_at", "status", "result_json",
    },
    "scan_events": {"event_id", "scan_id", "kind", "photo_id", "path", "error_json"},
    "image_embedding_profiles": {"profile_id", "profile_json", "created_at"},
    "image_embedding_results": {
        "result_id", "photo_id", "profile_id", "content_version", "thumbnail_profile",
        "input_image_hash", "vector", "vector_hash", "dimensions", "dtype", "normalized", "created_at",
    },
    "image_embedding_runs": {
        "run_id", "profile_id", "digest", "plan_json", "status", "confirmed_digest", "confirmed_at",
        "created_at", "updated_at", "model_calls",
    },
    "image_embedding_items": {
        "run_id", "photo_id", "profile_id", "content_version", "thumbnail_profile", "input_image_hash",
        "action", "snapshot_json", "status", "result_id", "attempts_json", "error_json", "updated_at",
    },
    "image_embedding_claims": {
        "photo_id", "profile_id", "content_version", "thumbnail_profile", "input_image_hash",
        "run_id", "created_at",
    },
    "image_embedding_settings": {"setting_key", "profile_id"},
    "virtual_folders": {"folder_id", "name", "name_key", "description", "created_at", "updated_at"},
    "virtual_folder_photos": {"folder_id", "photo_id", "added_at"},
}

PHOTO_REQUIRED_FIELDS = (
    "photo_id", "original_absolute_path", "content_version", "thumbnail_profile",
    "size_bytes", "mtime_ns", "metadata", "ingest_state", "original_status", "created_at", "updated_at",
)
PHOTO_OPTIONAL_FIELDS = (
    "original_relative_path", "last_ingest_error", "last_path_error", "last_original_check", "path_updated_at",
)


def _exclusive_file(path: Path) -> os.stat_result:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError as exc:
        raise PhotographyError("DATABASE_EXISTS", f"Album file already exists; it was not overwritten: {path}") from exc
    try:
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def _remove_owned_file(path: Path, identity: os.stat_result) -> None:
    try:
        current = path.stat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
        path.unlink()


def _new_destination(path):
    destination = Config(path).database_path
    if os.path.lexists(Path(path).expanduser()) or os.path.lexists(destination):
        raise PhotographyError("DATABASE_EXISTS", f"Album file already exists; it was not overwritten: {path}")
    return destination


def _publish_new(temporary: Path, destination: Path) -> None:
    try:
        if os.name == "nt":
            # Windows rename fails rather than replacing an existing destination.
            os.rename(temporary, destination)
        else:
            # POSIX rename replaces destinations; linking publishes exclusively instead.
            os.link(temporary, destination)
            temporary.unlink()
    except FileExistsError as exc:
        raise PhotographyError("DATABASE_EXISTS", f"Destination appeared during creation and was not overwritten: {destination}") from exc


class SQLiteStorage(ImageEmbeddingStorage, VirtualFolderStorage):
    def __init__(self, *args, **kwargs):
        raise PhotographyError("STORAGE_OPEN_REQUIRED", "Use SQLiteStorage.create(path) or SQLiteStorage.open(path).")

    @classmethod
    def _connect(cls, database_path: Path, *, writable: bool) -> SQLiteStorage:
        store = cls.__new__(cls)
        store.database_path = database_path
        store.writable = writable
        store.db = sqlite3.connect(
            database_path.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
            uri=True, timeout=5, isolation_level=None,
        )
        try:
            store.db.row_factory = sqlite3.Row
            store.db.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            store.close()
            raise
        return store

    @classmethod
    def create(cls, path: Path | str) -> SQLiteStorage:
        database_path = _new_destination(path)
        temporary = database_path.parent / (".sa-" + uuid4().hex + ".tmp")
        store = None
        identity = None
        try:
            identity = _exclusive_file(temporary)
            store = cls._connect(temporary, writable=True)
            store.db.execute("PRAGMA journal_mode=DELETE")
            with store.transaction():
                store.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                store.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                for statement in (*SCHEMA, *IMAGE_EMBEDDING_SCHEMA, *VIRTUAL_FOLDER_SCHEMA):
                    store.db.execute(statement)
                store.db.execute("INSERT INTO album_metadata VALUES (1,?,?)", (str(uuid4()), now()))
                store._validate_format()
            album_id = store.album()["id"]
            store.close()
            store = None
            _publish_new(temporary, database_path)
            store = cls.open(database_path, writable=True)
            if store.album()["id"] != album_id:
                raise PhotographyError("ALBUM_CHANGED", "The newly created album was replaced before it could be opened.")
            return store
        except BaseException as exc:
            if store is not None:
                store.close()
            if isinstance(exc, (OSError, sqlite3.Error)):
                raise PhotographyError("STORAGE_UNAVAILABLE", f"Could not create album file: {exc}") from exc
            raise
        finally:
            if identity is not None:
                _remove_owned_file(temporary, identity)

    @classmethod
    def open(cls, path: Path | str, writable: bool = False) -> SQLiteStorage:
        database_path = Config(path).database_path
        store = None
        try:
            try:
                info = database_path.stat()
            except (FileNotFoundError, NotADirectoryError):
                raise PhotographyError("DATABASE_NOT_FOUND", f"Album file does not exist: {database_path}")
            if not stat.S_ISREG(info.st_mode):
                raise PhotographyError("INVALID_DATABASE_PATH", "The selected album path is not a regular file.")
            store = cls._connect(database_path, writable=writable)
            with store.read_snapshot():
                store._validate_format()
            return store
        except BaseException as exc:
            if store is not None:
                store.close()
            if isinstance(exc, sqlite3.DatabaseError):
                code = "DATABASE_INVALID" if not isinstance(exc, sqlite3.OperationalError) else "STORAGE_UNAVAILABLE"
                raise PhotographyError(code, f"Could not open album file: {exc}") from exc
            if isinstance(exc, OSError):
                raise PhotographyError("STORAGE_UNAVAILABLE", f"Could not open album file: {exc}") from exc
            raise

    def _validate_format(self) -> None:
        application_id = self.db.execute("PRAGMA application_id").fetchone()[0]
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if 0 < version < SCHEMA_VERSION:
            raise PhotographyError("SCHEMA_UNSUPPORTED",
                f"Old database format v{version} is not supported. Create a new album; this file was not migrated.")
        if application_id != APPLICATION_ID:
            raise PhotographyError("ALBUM_FORMAT_INVALID", "This is not a supported Smart Albums file. Create a new album.")
        if version != SCHEMA_VERSION:
            raise PhotographyError("SCHEMA_UNSUPPORTED", f"Unsupported album schema version: {version}.")
        tables = {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        expected = {
            "album_metadata", "photos", "thumbnails", "scans", "scan_events",
            *IMAGE_EMBEDDING_TABLES, *VIRTUAL_FOLDER_TABLES,
        }
        if tables != expected:
            raise PhotographyError("SCHEMA_INVALID",
                f"Invalid album tables (missing: {sorted(expected - tables)}; unexpected: {sorted(tables - expected)}).")
        for table in expected:
            columns = {row["name"] for row in self.db.execute(f'PRAGMA table_info("{table}")')}
            missing = REQUIRED_COLUMNS[table] - columns
            if missing:
                raise PhotographyError("SCHEMA_INVALID", f"Album table {table} is missing required columns: {sorted(missing)}.")
        records = self.db.execute("SELECT singleton,album_uuid,created_at FROM album_metadata").fetchall()
        if len(records) != 1 or records[0]["singleton"] != 1:
            raise PhotographyError("SCHEMA_INVALID", "Album metadata must contain exactly one singleton record.")
        try:
            UUID(records[0]["album_uuid"])
            datetime.fromisoformat(records[0]["created_at"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise PhotographyError("SCHEMA_INVALID", "Album metadata has an invalid UUID or creation timestamp.") from exc
        if [row[0] for row in self.db.execute("PRAGMA quick_check")] != ["ok"]:
            raise PhotographyError("DATABASE_INVALID", "Album file failed SQLite integrity validation.")
        if self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise PhotographyError("SCHEMA_INVALID", "Album file contains invalid foreign-key references.")

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self) -> SQLiteStorage:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def assert_writable(self) -> None:
        if not self.writable:
            raise PhotographyError("STORAGE_READ_ONLY", "This album is open read-only; explicitly open it writable to make changes.")

    @contextmanager
    def transaction(self):
        self.assert_writable()
        if self.db.in_transaction:
            with self.savepoint():
                yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    @contextmanager
    def savepoint(self):
        self.assert_writable()
        key = "sp_" + uuid4().hex
        self.db.execute(f"SAVEPOINT {key}")
        try:
            yield
            self.db.execute(f"RELEASE SAVEPOINT {key}")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {key}")
                self.db.execute(f"RELEASE SAVEPOINT {key}")
            raise

    @contextmanager
    def read_snapshot(self):
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN")
        try:
            yield
        finally:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")

    def album(self) -> dict:
        row = self.db.execute("SELECT album_uuid,created_at FROM album_metadata WHERE singleton=1").fetchone()
        return {
            "id": row["album_uuid"], "name": self.database_path.stem,
            "database_path": str(self.database_path), "created_at": row["created_at"],
        }

    def backup(self, output: Path | str) -> dict:
        destination_path = _new_destination(output)
        if self.db.in_transaction:
            raise PhotographyError("STORAGE_BUSY", "Finish the active transaction before backing up this album.")
        identity = None
        temporary = destination_path.parent / (".sa-" + uuid4().hex + ".tmp")
        try:
            identity = _exclusive_file(temporary)
            with closing(sqlite3.connect(temporary.as_uri() + "?mode=rw", uri=True)) as destination:
                self.db.backup(destination)
            size_bytes = temporary.stat().st_size
            _publish_new(temporary, destination_path)
            return {"album": self.album(), "output": str(destination_path),
                    "size_bytes": size_bytes}
        except BaseException as exc:
            if isinstance(exc, (OSError, sqlite3.Error)):
                raise PhotographyError("BACKUP_FAILED", f"Could not back up album file: {exc}") from exc
            raise
        finally:
            if identity is not None:
                _remove_owned_file(temporary, identity)

    @staticmethod
    def _photo(row: sqlite3.Row) -> dict:
        photo = dict(row)
        photo["metadata"] = json.loads(photo.pop("metadata_json"))
        for field in ("last_ingest_error", "last_path_error"):
            photo[field] = json.loads(photo[field]) if photo[field] is not None else None
        return photo

    def photo(self, photo_id: str) -> dict:
        row = self.db.execute("SELECT * FROM photos WHERE photo_id=?", (photo_id,)).fetchone()
        if row is None:
            raise PhotographyError("PHOTO_NOT_FOUND", "Photo does not exist in this album.")
        return self._photo(row)

    def photos(self) -> list[dict]:
        return [self._photo(row) for row in self.db.execute("SELECT * FROM photos ORDER BY photo_id")]

    def put_photo(self, photo: dict) -> None:
        self.assert_writable()
        missing = [field for field in PHOTO_REQUIRED_FIELDS if field not in photo or photo[field] is None]
        if missing:
            raise PhotographyError("INVALID_PHOTO", f"Photo is missing required fields: {missing}.")
        values = {field: photo[field] for field in PHOTO_REQUIRED_FIELDS}
        values.update({field: photo.get(field) for field in PHOTO_OPTIONAL_FIELDS})
        for field in ("metadata", "last_ingest_error", "last_path_error"):
            value = values.pop(field)
            if value is not None and not isinstance(value, dict):
                raise PhotographyError("INVALID_PHOTO", f"Photo {field} must be an object or null.")
            try:
                values["metadata_json" if field == "metadata" else field] = (
                    json.dumps(value, ensure_ascii=False, allow_nan=False) if value is not None else None)
            except (TypeError, ValueError) as exc:
                raise PhotographyError("INVALID_PHOTO", f"Photo {field} must contain finite JSON values.") from exc
        columns = ",".join(values)
        parameters = ",".join(":" + field for field in values)
        updates = ",".join(f"{field}=excluded.{field}" for field in values if field != "photo_id")
        self.db.execute(
            f"INSERT INTO photos ({columns}) VALUES ({parameters}) ON CONFLICT(photo_id) DO UPDATE SET {updates}",
            values,
        )

    def put_thumbnail(self, photo: dict, data: bytes) -> None:
        self.assert_writable()
        width, height = validate_preview(data)
        self.db.execute("""INSERT INTO thumbnails VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(photo_id) DO UPDATE SET content_version=excluded.content_version,
            profile=excluded.profile,image_hash=excluded.image_hash,mime_type=excluded.mime_type,
            width=excluded.width,height=excluded.height,created_at=excluded.created_at,
            size_bytes=excluded.size_bytes,data=excluded.data""",
            (photo["photo_id"], photo["content_version"], photo["thumbnail_profile"],
             hashlib.sha256(data).hexdigest(), "image/jpeg", width, height, now(), len(data), data))

    def thumbnail(self, photo_id: str, *, include_data: bool = True) -> dict:
        fields = "*" if include_data else "photo_id,content_version,profile,image_hash,mime_type,width,height,created_at,size_bytes"
        row = self.db.execute(f"SELECT {fields} FROM thumbnails WHERE photo_id=?", (photo_id,)).fetchone()
        if row is None:
            raise PhotographyError("INVALID_PREVIEW", "Stored preview does not exist; rescan to repair it.")
        return dict(row)

    def start_scan(self, scan_id: str, source_absolute_path: str, source_relative_path: str | None = None) -> None:
        self.assert_writable()
        self.db.execute("""INSERT INTO scans
            (scan_id,source_absolute_path,source_relative_path,started_at,status) VALUES (?,?,?,?,'running')""",
            (scan_id, source_absolute_path, source_relative_path, now()))

    def event(self, scan_id: str, kind: str, photo_id: str | None, path: str, error: dict | None = None) -> None:
        self.assert_writable()
        self.db.execute("INSERT INTO scan_events(scan_id,kind,photo_id,path,error_json) VALUES (?,?,?,?,?)",
            (scan_id, kind, photo_id, path, json.dumps(error, ensure_ascii=False) if error is not None else None))

    def finish_scan(self, scan_id: str, result: dict) -> None:
        self.assert_writable()
        cursor = self.db.execute("UPDATE scans SET completed_at=?,status=?,result_json=? WHERE scan_id=?",
            (now(), result["status"], json.dumps(result, ensure_ascii=False), scan_id))
        if cursor.rowcount != 1:
            raise PhotographyError("SCAN_NOT_FOUND", "Scan does not exist in this album.")

    def scan(self, scan_id: str) -> dict:
        row = self.db.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
        if row is None:
            raise PhotographyError("SCAN_NOT_FOUND", "Scan does not exist in this album.")
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json")) if row["result_json"] is not None else None
        return result

    @staticmethod
    def _limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise PhotographyError("INVALID_ARGUMENT", "Page limit must be between 1 and 1000.")

    def events(self, scan_id: str, limit: int = 100, after: int = 0, changes_only: bool = False) -> dict:
        self._limit(limit)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise PhotographyError("INVALID_ARGUMENT", "Event cursor must be a nonnegative integer.")
        self.scan(scan_id)
        clause = " AND kind<>'unchanged'" if changes_only else ""
        rows = self.db.execute(f"""SELECT * FROM scan_events WHERE scan_id=? AND event_id>?
            {clause} ORDER BY event_id LIMIT ?""", (scan_id, after, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            item["error"] = json.loads(item.pop("error_json")) if row["error_json"] is not None else None
            items.append(item)
        return {"items": items, "next_cursor": items[-1]["event_id"] if len(rows) > limit else None}
