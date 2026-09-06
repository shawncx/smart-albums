"""Explicit archive fixtures, never generated observations or production data."""
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

# Old schema exists only in test fixtures, never in new production databases.
LEGACY_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS analyses (analysis_id TEXT PRIMARY KEY, photo_id TEXT NOT NULL REFERENCES photos(photo_id), cache_key TEXT NOT NULL, created_at TEXT NOT NULL, data_json TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS analyses_cache ON analyses(photo_id,cache_key,created_at)",
    "CREATE TABLE IF NOT EXISTS analysis_runs (run_id TEXT PRIMARY KEY, status TEXT NOT NULL, data_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS embedding_encoders (encoder_id TEXT PRIMARY KEY, profile_json TEXT NOT NULL, created_at TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS photo_embeddings (
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        encoder_id TEXT NOT NULL REFERENCES embedding_encoders(encoder_id),
        analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id),
        content_version TEXT NOT NULL, text_hash TEXT NOT NULL, recipe_version TEXT NOT NULL,
        dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
        dtype TEXT NOT NULL CHECK(dtype='float32-le'), normalized INTEGER NOT NULL CHECK(normalized=1),
        vector BLOB NOT NULL, vector_hash TEXT NOT NULL, token_count INTEGER NOT NULL,
        truncated INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(photo_id,encoder_id))""",
    "CREATE INDEX IF NOT EXISTS embeddings_encoder ON photo_embeddings(encoder_id,photo_id)",
    "CREATE TABLE IF NOT EXISTS analysis_settings (id TEXT PRIMARY KEY, data_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS analysis_plans (id TEXT PRIMARY KEY, data_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS analysis_plan_items (plan_id TEXT NOT NULL REFERENCES analysis_plans(id), photo_id TEXT NOT NULL, data_json TEXT NOT NULL, PRIMARY KEY(plan_id,photo_id))",
    "CREATE TABLE IF NOT EXISTS analysis_requests (id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES analysis_plans(id), data_json TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS requests_plan ON analysis_requests(plan_id)",
    "CREATE TABLE IF NOT EXISTS analysis_claims (cache_key TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES analysis_requests(id))",
)


def create_legacy_schema(db):
    for statement in LEGACY_SCHEMA:
        db.execute(statement)


def seed_observation(store, photo_id):
    create_legacy_schema(store.db)
    photo = store.photo(photo_id)
    record = {
        "analysis_id": "archive_" + photo_id, "photo_id": photo_id,
        "cache_key": "synthetic-archive-key", "created_at": "2026-01-01T00:00:00Z",
        "content_version": photo["content_version"], "thumbnail_profile": photo["thumbnail_profile"],
        "input_image_hash": store.thumbnail(photo_id, include_data=False)["image_hash"],
        "model": "synthetic-archive-fixture",
        "data": {"visual_description": "Synthetic historical observation, not a model result."},
    }
    store.db.execute("INSERT INTO analyses VALUES (?,?,?,?,?)",
                     (record["analysis_id"], photo_id, record["cache_key"], record["created_at"],
                      json.dumps(record, ensure_ascii=False)))
    return record
