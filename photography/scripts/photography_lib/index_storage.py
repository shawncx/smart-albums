"""Versioned image-index results, plans, progress and concurrency claims."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .fingerprints import fingerprint
from .config import PhotographyError


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def profile_identity(profile):
    if not isinstance(profile, dict) or not profile:
        raise PhotographyError("INDEX_PROFILE_INVALID", "An image-index profile must be a nonempty object.")
    try:
        encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if json.loads(encoded) != profile:
            raise ValueError("Profile must contain JSON values.")
    except (TypeError, ValueError) as exc:
        raise PhotographyError("INDEX_PROFILE_INVALID", "The image-index profile must contain finite JSON values.") from exc
    return fingerprint(profile), encoded


INDEX_TABLES = (
    "image_index_profiles", "image_index_results", "image_index_runs",
    "image_index_items", "image_index_claims", "image_index_settings",
)

INDEX_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS image_index_profiles (
        profile_id TEXT PRIMARY KEY, profile_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS image_index_results (
        result_id TEXT PRIMARY KEY,
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        profile_id TEXT NOT NULL REFERENCES image_index_profiles(profile_id),
        content_version TEXT NOT NULL, thumbnail_profile TEXT NOT NULL, input_image_hash TEXT NOT NULL,
        vector BLOB NOT NULL, vector_hash TEXT NOT NULL,
        dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
        dtype TEXT NOT NULL CHECK(dtype='float32-le'),
        normalized INTEGER NOT NULL CHECK(normalized=1), created_at TEXT NOT NULL,
        UNIQUE(photo_id,profile_id,content_version,thumbnail_profile,input_image_hash),
        UNIQUE(result_id,photo_id,profile_id,content_version,thumbnail_profile,input_image_hash))""",
    "CREATE INDEX IF NOT EXISTS image_results_profile ON image_index_results(profile_id,photo_id)",
    """CREATE TABLE IF NOT EXISTS image_index_runs (
        run_id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES image_index_profiles(profile_id),
        digest TEXT NOT NULL, plan_json TEXT NOT NULL, status TEXT NOT NULL,
        confirmed_digest TEXT CHECK(confirmed_digest IS NULL OR confirmed_digest=digest),
        confirmed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        model_calls INTEGER NOT NULL DEFAULT 0 CHECK(model_calls>=0),
        UNIQUE(run_id,profile_id))""",
    """CREATE TABLE IF NOT EXISTS image_index_items (
        run_id TEXT NOT NULL, photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        profile_id TEXT NOT NULL REFERENCES image_index_profiles(profile_id),
        content_version TEXT, thumbnail_profile TEXT, input_image_hash TEXT,
        action TEXT NOT NULL CHECK(action IN ('reuse','encode','skip')),
        snapshot_json TEXT NOT NULL, status TEXT NOT NULL, result_id TEXT,
        attempts_json TEXT NOT NULL, error_json TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(run_id,photo_id),
        UNIQUE(run_id,photo_id,profile_id,content_version,thumbnail_profile,input_image_hash),
        FOREIGN KEY(run_id,profile_id) REFERENCES image_index_runs(run_id,profile_id),
        FOREIGN KEY(result_id,photo_id,profile_id,content_version,thumbnail_profile,input_image_hash)
            REFERENCES image_index_results(result_id,photo_id,profile_id,content_version,thumbnail_profile,input_image_hash),
        CHECK(result_id IS NULL OR (content_version IS NOT NULL AND thumbnail_profile IS NOT NULL
            AND input_image_hash IS NOT NULL)))""",
    """CREATE TABLE IF NOT EXISTS image_index_claims (
        photo_id TEXT NOT NULL, profile_id TEXT NOT NULL, content_version TEXT NOT NULL,
        thumbnail_profile TEXT NOT NULL, input_image_hash TEXT NOT NULL,
        run_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(photo_id,profile_id,content_version,thumbnail_profile,input_image_hash),
        FOREIGN KEY(run_id,photo_id,profile_id,content_version,thumbnail_profile,input_image_hash)
            REFERENCES image_index_items(run_id,photo_id,profile_id,content_version,thumbnail_profile,input_image_hash))""",
    """CREATE TABLE IF NOT EXISTS image_index_settings (
        setting_key TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES image_index_profiles(profile_id))""",
    """CREATE TRIGGER IF NOT EXISTS image_profile_immutable
        BEFORE UPDATE ON image_index_profiles
        BEGIN SELECT RAISE(ABORT,'Image-index profiles are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS image_plan_immutable
        BEFORE UPDATE OF run_id,profile_id,digest,plan_json,created_at ON image_index_runs
        BEGIN SELECT RAISE(ABORT,'Image-index plans are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS image_snapshot_immutable
        BEFORE UPDATE OF run_id,photo_id,profile_id,content_version,thumbnail_profile,input_image_hash,
            action,snapshot_json ON image_index_items
        BEGIN SELECT RAISE(ABORT,'Image-index input snapshots are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS image_result_identity_immutable
        BEFORE UPDATE OF result_id,photo_id,profile_id,content_version,thumbnail_profile,input_image_hash
            ON image_index_results
        BEGIN SELECT RAISE(ABORT,'Image-index result identities are immutable'); END""",
)


class IndexStorage:
    def put_index_profile(self, profile):
        profile_id, encoded = profile_identity(profile)
        with self.savepoint():
            self.db.execute("""INSERT INTO image_index_profiles VALUES (?,?,?)
                ON CONFLICT(profile_id) DO NOTHING""", (profile_id, encoded, timestamp()))
            if self.index_profile(profile_id) != profile:
                raise PhotographyError("INDEX_PROFILE_INVALID", "Stored profile does not match its fingerprint.")
        return profile_id

    def index_profile(self, profile_id):
        row = self.db.execute(
            "SELECT profile_json FROM image_index_profiles WHERE profile_id=?", (profile_id,)).fetchone()
        if row is None:
            raise PhotographyError("INDEX_PROFILE_NOT_FOUND", "Image-index profile does not exist.")
        try:
            profile = json.loads(row[0])
            if profile_identity(profile)[0] != profile_id:
                raise ValueError("Fingerprint mismatch")
        except (ValueError, TypeError) as exc:
            raise PhotographyError("INDEX_PROFILE_INVALID", "Stored profile failed its fingerprint check.") from exc
        return profile

    def index_profiles(self):
        return [{"profile_id": row["profile_id"], "profile": self.index_profile(row["profile_id"]),
                 "created_at": row["created_at"]}
                for row in self.db.execute(
                    "SELECT profile_id,created_at FROM image_index_profiles ORDER BY created_at,profile_id")]

    def default_index_profile(self):
        row = self.db.execute(
            "SELECT profile_id FROM image_index_settings WHERE setting_key='default_profile'").fetchone()
        return row[0] if row else None

    def set_default_index_profile(self, profile_id):
        with self.savepoint():
            self.index_profile(profile_id)
            self.db.execute("""INSERT INTO image_index_settings VALUES ('default_profile',?)
                ON CONFLICT(setting_key) DO UPDATE SET profile_id=excluded.profile_id""", (profile_id,))

    def index_photos(self, *, album_id=None, library_id=None):
        if album_id is not None and library_id is not None:
            raise PhotographyError("INVALID_ARGUMENT", "Choose an album or a library, not both.")
        if album_id is not None:
            return self.photos_for_album(album_id)
        if library_id is not None:
            if not self.db.execute("SELECT 1 FROM libraries WHERE library_id=?", (library_id,)).fetchone():
                raise PhotographyError("LIBRARY_NOT_FOUND", "Photo library does not exist.")
            return [json.loads(row[0]) for row in self.db.execute(
                "SELECT data_json FROM photos WHERE library_id=? ORDER BY photo_id", (library_id,))]
        return [json.loads(row[0]) for row in self.db.execute("SELECT data_json FROM photos ORDER BY photo_id")]

    @property
    def index_lock_path(self):
        path = self.db.execute("PRAGMA database_list").fetchone()[2]
        return Path(path).resolve().with_suffix(".image-index.lock")

    def _index_result(self, snapshot, profile_id):
        row = self.db.execute("""SELECT * FROM image_index_results WHERE
            photo_id=? AND profile_id=? AND content_version=? AND thumbnail_profile=? AND input_image_hash=?""",
            (snapshot["photo_id"], profile_id, snapshot["content_version"],
             snapshot["thumbnail_profile"], snapshot["input_image_hash"])).fetchone()
        return dict(row) if row else None

    def _has_index_results(self, photo_id, profile_id):
        return self.db.execute(
            "SELECT 1 FROM image_index_results WHERE photo_id=? AND profile_id=? LIMIT 1",
            (photo_id, profile_id)).fetchone() is not None

    def _put_index_result(self, snapshot, profile_id, blob, vector_hash, dimensions):
        old = self._index_result(snapshot, profile_id)
        result_id = old["result_id"] if old else "idx_" + uuid4().hex
        self.db.execute("""INSERT INTO image_index_results
            (result_id,photo_id,profile_id,content_version,thumbnail_profile,input_image_hash,
             vector,vector_hash,dimensions,dtype,normalized,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,'float32-le',1,?)
            ON CONFLICT(photo_id,profile_id,content_version,thumbnail_profile,input_image_hash) DO UPDATE SET
                vector=excluded.vector,vector_hash=excluded.vector_hash,dimensions=excluded.dimensions,
                dtype=excluded.dtype,normalized=excluded.normalized,created_at=excluded.created_at""",
            (result_id, snapshot["photo_id"], profile_id, snapshot["content_version"],
             snapshot["thumbnail_profile"], snapshot["input_image_hash"], blob, vector_hash, dimensions, timestamp()))
        return result_id

    def _create_index_run(self, plan):
        self.db.execute("""INSERT INTO image_index_runs
            (run_id,profile_id,digest,plan_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
            (plan["run_id"], plan["profile_id"], plan["digest"], json.dumps(plan, ensure_ascii=False),
             plan["status"], plan["created_at"], plan["created_at"]))
        for snapshot in plan["snapshots"]:
            self.db.execute("""INSERT INTO image_index_items
                (run_id,photo_id,profile_id,content_version,thumbnail_profile,input_image_hash,
                 action,snapshot_json,status,result_id,attempts_json,error_json,updated_at)
                 VALUES (?,?,?,?,?,?,?,?,?,NULL,'[]',NULL,?)""",
                (plan["run_id"], snapshot["photo_id"], plan["profile_id"], snapshot.get("content_version"),
                 snapshot.get("thumbnail_profile"), snapshot.get("input_image_hash"), snapshot["action"],
                 json.dumps(snapshot, ensure_ascii=False), "pending", timestamp()))

    def _index_run(self, run_id):
        row = self.db.execute("SELECT * FROM image_index_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise PhotographyError("INDEX_RUN_NOT_FOUND", "Image-index run does not exist.")
        result = dict(row)
        try:
            result["plan"] = json.loads(result.pop("plan_json"))
        except (TypeError, ValueError) as exc:
            raise PhotographyError("INDEX_PLAN_INVALID", "Stored image-index plan is invalid.") from exc
        return result

    def _index_items(self, run_id):
        items = []
        for row in self.db.execute("SELECT * FROM image_index_items WHERE run_id=? ORDER BY photo_id", (run_id,)):
            item = dict(row)
            try:
                item["snapshot"] = json.loads(item.pop("snapshot_json"))
                item["attempts"] = json.loads(item.pop("attempts_json"))
                error = item.pop("error_json")
                item["error"] = json.loads(error) if error else None
            except (ValueError, TypeError) as exc:
                raise PhotographyError("INDEX_PLAN_INVALID", "Stored image-index item is invalid.") from exc
            items.append(item)
        return items

    def _update_index_run(self, run_id, *, status=None, confirmed_digest=None, model_call=False):
        updates, values = ["updated_at=?"], [timestamp()]
        if status is not None:
            updates.append("status=?")
            values.append(status)
        if confirmed_digest is not None:
            updates.extend(("confirmed_digest=?", "confirmed_at=?"))
            values.extend((confirmed_digest, timestamp()))
        if model_call:
            updates.append("model_calls=model_calls+1")
        self.db.execute(f"UPDATE image_index_runs SET {','.join(updates)} WHERE run_id=?", (*values, run_id))

    def _update_index_item(self, run_id, item):
        self.db.execute("""UPDATE image_index_items SET
            status=?,result_id=?,attempts_json=?,error_json=?,updated_at=? WHERE run_id=? AND photo_id=?""",
            (item["status"], item.get("result_id"), json.dumps(item["attempts"], ensure_ascii=False),
             json.dumps(item["error"], ensure_ascii=False) if item.get("error") else None,
             timestamp(), run_id, item["photo_id"]))

    def _claim_index_input(self, run_id, snapshot, profile_id):
        row = self.db.execute("""SELECT run_id FROM image_index_claims WHERE
            photo_id=? AND profile_id=? AND content_version=? AND thumbnail_profile=? AND input_image_hash=?""",
            (snapshot["photo_id"], profile_id, snapshot["content_version"],
             snapshot["thumbnail_profile"], snapshot["input_image_hash"])).fetchone()
        if row:
            raise PhotographyError("INDEX_IN_PROGRESS", "This stored image input is already claimed.")
        self.db.execute("INSERT INTO image_index_claims VALUES (?,?,?,?,?,?,?)",
            (snapshot["photo_id"], profile_id, snapshot["content_version"],
             snapshot["thumbnail_profile"], snapshot["input_image_hash"], run_id, timestamp()))

    def _release_index_input(self, run_id, photo_id):
        self.db.execute("DELETE FROM image_index_claims WHERE run_id=? AND photo_id=?", (run_id, photo_id))

    def _recover_index_claims(self):
        # Called only while holding the database-wide OS lock, never on a timer.
        run_ids = [row[0] for row in self.db.execute(
            "SELECT run_id FROM image_index_runs WHERE status='running'")]
        for run_id in run_ids:
            for item in self._index_items(run_id):
                if item["status"] != "running":
                    continue
                error = {"code": "INDEX_INTERRUPTED", "message": "The previous executor exited before saving this input."}
                item.update(status="failed", error=error, result_id=None)
                if item["attempts"] and not item["attempts"][-1].get("completed_at"):
                    item["attempts"][-1].update(completed_at=timestamp(), error=error, status="interrupted")
                self._update_index_item(run_id, item)
            self._update_index_run(run_id, status="interrupted")
        self.db.execute("DELETE FROM image_index_claims")
