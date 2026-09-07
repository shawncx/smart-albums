"""Typed, immutable image-feature evidence and resumable stage-one jobs."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from uuid import uuid4

from .config import PhotographyError
from .fingerprints import fingerprint
from .image_vectors import pack_vector, unpack_vector


COMPONENT_NAMES = ("ocr", "objects", "scene", "color", "composition", "perceptual_hash")
FEATURE_ITEM_STATES = (
    "pending", "running", "cached", "computed", "failed", "stale", "invalid_input",
    "input_unavailable", "dependency_missing", "cancelled",
)
_COMPONENT_SQL = ",".join(repr(value) for value in COMPONENT_NAMES)
IMAGE_FEATURE_TABLES = (
    "image_feature_profiles", "image_feature_results", "image_feature_dependencies",
    "image_feature_embedding_dependencies", "image_feature_settings", "image_feature_runs",
    "image_feature_items", "image_feature_claims", "image_ocr_documents", "image_ocr_blocks",
    "image_object_instances", "image_scene_scores", "image_scene_prototype_sets",
    "image_scene_prototypes", "image_color_features", "image_color_palette",
    "image_composition_features", "image_perceptual_hashes", "image_similarity_pairs",
)
FEATURE_FTS_TABLE = "image_ocr_fts"
FEATURE_FTS_SHADOW_TABLES = tuple(FEATURE_FTS_TABLE + suffix for suffix in (
    "_data", "_idx", "_docsize", "_config"))
FEATURE_ALL_TABLES = (*IMAGE_FEATURE_TABLES, FEATURE_FTS_TABLE, *FEATURE_FTS_SHADOW_TABLES)


def _typed_table(name, component, columns, *, many=False):
    return f"""CREATE TABLE {name} (
        result_id TEXT NOT NULL,
        component TEXT NOT NULL DEFAULT '{component}' CHECK(component='{component}'),
        {columns},
        PRIMARY KEY(result_id{',ordinal' if many else ''}),
        FOREIGN KEY(result_id,component) REFERENCES image_feature_results(result_id,component))"""


IMAGE_FEATURE_SCHEMA = (
    f"""CREATE TABLE image_feature_profiles (
        profile_id TEXT PRIMARY KEY NOT NULL,
        component TEXT NOT NULL CHECK(component IN ({_COMPONENT_SQL})),
        input_scope TEXT NOT NULL CHECK(input_scope IN ('original','stored_thumbnail','image_embedding','object_result')),
        output_schema_version INTEGER NOT NULL CHECK(output_schema_version=1),
        profile_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(profile_id,component), UNIQUE(profile_id,component,input_scope,output_schema_version))""",
    """CREATE TABLE image_feature_results (
        result_id TEXT PRIMARY KEY NOT NULL, photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        profile_id TEXT NOT NULL, component TEXT NOT NULL, content_version TEXT NOT NULL,
        input_scope TEXT NOT NULL, input_fingerprint TEXT NOT NULL, input_manifest_json TEXT NOT NULL,
        output_schema_version INTEGER NOT NULL, payload_hash TEXT NOT NULL,
        complete INTEGER NOT NULL CHECK(complete IN (0,1)),
        width INTEGER CHECK(width>0), height INTEGER CHECK(height>0),
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        CHECK((width IS NULL)=(height IS NULL)),
        CHECK(input_scope='image_embedding' OR (width IS NOT NULL AND height IS NOT NULL)),
        UNIQUE(photo_id,profile_id,input_fingerprint), UNIQUE(result_id,component),
        UNIQUE(result_id,photo_id), UNIQUE(result_id,photo_id,profile_id,content_version,payload_hash),
        UNIQUE(result_id,photo_id,profile_id,input_fingerprint),
        UNIQUE(result_id,photo_id,profile_id,content_version,component),
        FOREIGN KEY(profile_id,component,input_scope,output_schema_version)
            REFERENCES image_feature_profiles(profile_id,component,input_scope,output_schema_version))""",
    "CREATE INDEX image_feature_history ON image_feature_results(photo_id,profile_id,result_id)",
    """CREATE TABLE image_feature_dependencies (
        result_id TEXT NOT NULL, photo_id TEXT NOT NULL,
        component TEXT NOT NULL DEFAULT 'composition' CHECK(component='composition'),
        source_result_id TEXT NOT NULL, source_profile_id TEXT NOT NULL,
        source_component TEXT NOT NULL DEFAULT 'objects' CHECK(source_component='objects'),
        content_version TEXT NOT NULL, source_payload_hash TEXT NOT NULL,
        PRIMARY KEY(result_id,source_result_id), UNIQUE(result_id),
        CHECK(result_id<>source_result_id),
        FOREIGN KEY(result_id,photo_id) REFERENCES image_feature_results(result_id,photo_id),
        FOREIGN KEY(result_id,component) REFERENCES image_feature_results(result_id,component),
        FOREIGN KEY(source_result_id,source_component) REFERENCES image_feature_results(result_id,component),
        FOREIGN KEY(source_result_id,photo_id,source_profile_id,content_version,source_payload_hash)
            REFERENCES image_feature_results(result_id,photo_id,profile_id,content_version,payload_hash))""",
    """CREATE TABLE image_feature_embedding_dependencies (
        result_id TEXT PRIMARY KEY NOT NULL, photo_id TEXT NOT NULL,
        component TEXT NOT NULL DEFAULT 'scene' CHECK(component='scene'),
        source_result_id TEXT NOT NULL, source_profile_id TEXT NOT NULL,
        content_version TEXT NOT NULL, thumbnail_profile TEXT NOT NULL, input_image_hash TEXT NOT NULL,
        vector_hash TEXT NOT NULL,
        FOREIGN KEY(result_id,photo_id) REFERENCES image_feature_results(result_id,photo_id),
        FOREIGN KEY(result_id,component) REFERENCES image_feature_results(result_id,component),
        FOREIGN KEY(source_result_id,photo_id,source_profile_id,content_version,thumbnail_profile,input_image_hash)
            REFERENCES image_embedding_results(result_id,photo_id,profile_id,content_version,thumbnail_profile,input_image_hash))""",
    """CREATE TABLE image_feature_settings (
        component TEXT PRIMARY KEY NOT NULL, profile_id TEXT NOT NULL,
        FOREIGN KEY(profile_id,component) REFERENCES image_feature_profiles(profile_id,component))""",
    """CREATE TABLE image_feature_runs (
        run_id TEXT PRIMARY KEY NOT NULL, work_kind TEXT NOT NULL CHECK(work_kind IN ('extract','prototypes','compare')),
        profile_id TEXT NOT NULL, component TEXT NOT NULL, digest TEXT NOT NULL, plan_json TEXT NOT NULL,
        status TEXT NOT NULL, confirmed_digest TEXT CHECK(confirmed_digest IS NULL OR confirmed_digest=digest),
        confirmed_at TEXT, model_calls INTEGER NOT NULL DEFAULT 0 CHECK(model_calls>=0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(run_id,profile_id,work_kind),
        CHECK(work_kind<>'prototypes' OR component='scene'),
        CHECK(work_kind<>'compare' OR component='perceptual_hash'),
        FOREIGN KEY(profile_id,component) REFERENCES image_feature_profiles(profile_id,component))""",
    """CREATE TABLE image_feature_items (
        run_id TEXT NOT NULL, item_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        photo_id TEXT REFERENCES photos(photo_id), profile_id TEXT NOT NULL, work_kind TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('compute','reuse','skip')),
        snapshot_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','running','cached','computed','failed','stale',
            'invalid_input','input_unavailable','dependency_missing','cancelled')),
        attempts_json TEXT NOT NULL,
        error_json TEXT, result_id TEXT, progress_json TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(run_id,item_id), UNIQUE(run_id,ordinal),
        UNIQUE(run_id,item_id,profile_id,input_fingerprint),
        CHECK((work_kind='extract' AND photo_id IS NOT NULL) OR (work_kind<>'extract' AND photo_id IS NULL)),
        FOREIGN KEY(run_id,profile_id,work_kind) REFERENCES image_feature_runs(run_id,profile_id,work_kind))""",
    """CREATE TABLE image_feature_claims (
        profile_id TEXT NOT NULL, input_fingerprint TEXT NOT NULL, scope_key TEXT NOT NULL,
        run_id TEXT NOT NULL, item_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(profile_id,input_fingerprint,scope_key), UNIQUE(run_id,item_id),
        FOREIGN KEY(run_id,item_id,profile_id,input_fingerprint)
            REFERENCES image_feature_items(run_id,item_id,profile_id,input_fingerprint))""",
    """CREATE TABLE image_ocr_documents (
        document_id INTEGER PRIMARY KEY, result_id TEXT NOT NULL UNIQUE,
        component TEXT NOT NULL DEFAULT 'ocr' CHECK(component='ocr'),
        text TEXT NOT NULL, normalized_text TEXT NOT NULL, block_count INTEGER NOT NULL CHECK(block_count>=0),
        FOREIGN KEY(result_id,component) REFERENCES image_feature_results(result_id,component))""",
    _typed_table("image_ocr_blocks", "ocr", """ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        text TEXT NOT NULL, polygon_json TEXT NOT NULL, recognition_score REAL,
        detection_score REAL""", many=True),
    _typed_table("image_object_instances", "objects", """ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        class_id TEXT NOT NULL, score REAL NOT NULL,
        x1 REAL NOT NULL, y1 REAL NOT NULL, x2 REAL NOT NULL, y2 REAL NOT NULL""", many=True),
    "CREATE INDEX image_objects_class ON image_object_instances(class_id,score,result_id)",
    _typed_table("image_scene_scores", "scene", """ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        scene_id TEXT NOT NULL, score REAL NOT NULL, UNIQUE(result_id,scene_id)""", many=True),
    "CREATE INDEX image_scenes_score ON image_scene_scores(scene_id,score,result_id)",
    """CREATE TABLE image_scene_prototype_sets (
        set_id TEXT PRIMARY KEY NOT NULL, profile_id TEXT NOT NULL UNIQUE,
        component TEXT NOT NULL DEFAULT 'scene' CHECK(component='scene'),
        embedding_profile_id TEXT NOT NULL REFERENCES image_embedding_profiles(profile_id),
        payload_hash TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(set_id,profile_id),
        FOREIGN KEY(profile_id,component) REFERENCES image_feature_profiles(profile_id,component))""",
    """CREATE TABLE image_scene_prototypes (
        set_id TEXT NOT NULL REFERENCES image_scene_prototype_sets(set_id),
        ordinal INTEGER NOT NULL CHECK(ordinal>=0), scene_id TEXT NOT NULL, label TEXT NOT NULL,
        prompts_json TEXT NOT NULL, vector BLOB NOT NULL, vector_hash TEXT NOT NULL,
        dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
        PRIMARY KEY(set_id,ordinal), UNIQUE(set_id,scene_id))""",
    _typed_table("image_color_features", "color", """mean_saturation REAL NOT NULL,
        low_saturation_fraction REAL NOT NULL, hue_histogram_json TEXT NOT NULL"""),
    _typed_table("image_color_palette", "color", """ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        red INTEGER NOT NULL, green INTEGER NOT NULL, blue INTEGER NOT NULL, fraction REAL NOT NULL""", many=True),
    _typed_table("image_composition_features", "composition", """subject_index INTEGER,
        center_x REAL, center_y REAL, subject_area REAL, thirds_distance REAL,
        union_area REAL NOT NULL, bounding_area REAL NOT NULL, uncovered_fraction REAL NOT NULL"""),
    _typed_table("image_perceptual_hashes", "perceptual_hash", """algorithm TEXT NOT NULL,
        bits INTEGER NOT NULL CHECK(bits>0 AND bits%8=0), hash BLOB NOT NULL CHECK(length(hash)*8=bits)"""),
    """CREATE TABLE image_similarity_pairs (
        pair_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
        work_kind TEXT NOT NULL DEFAULT 'compare' CHECK(work_kind='compare'), profile_id TEXT NOT NULL,
        photo_id_a TEXT NOT NULL REFERENCES photos(photo_id), photo_id_b TEXT NOT NULL REFERENCES photos(photo_id),
        result_id_a TEXT, result_id_b TEXT, content_version_a TEXT NOT NULL, content_version_b TEXT NOT NULL,
        component TEXT NOT NULL DEFAULT 'perceptual_hash' CHECK(component='perceptual_hash'),
        metric TEXT NOT NULL CHECK(metric IN ('exact','hamming')), distance REAL NOT NULL CHECK(distance>=0),
        CHECK(photo_id_a<photo_id_b),
        CHECK((metric='exact' AND result_id_a IS NULL AND result_id_b IS NULL AND distance=0
            AND content_version_a=content_version_b) OR
            (metric='hamming' AND result_id_a IS NOT NULL AND result_id_b IS NOT NULL)),
        UNIQUE(run_id,photo_id_a,photo_id_b,metric),
        FOREIGN KEY(run_id,profile_id,work_kind) REFERENCES image_feature_runs(run_id,profile_id,work_kind),
        FOREIGN KEY(result_id_a,photo_id_a,profile_id,content_version_a,component)
            REFERENCES image_feature_results(result_id,photo_id,profile_id,content_version,component),
        FOREIGN KEY(result_id_b,photo_id_b,profile_id,content_version_b,component)
            REFERENCES image_feature_results(result_id,photo_id,profile_id,content_version,component))""",
    """CREATE VIRTUAL TABLE image_ocr_fts USING fts5(normalized_text,
        content='image_ocr_documents', content_rowid='document_id', tokenize='trigram')""",
    """CREATE TRIGGER image_ocr_fts_insert AFTER INSERT ON image_ocr_documents BEGIN
        INSERT INTO image_ocr_fts(rowid,normalized_text) VALUES(new.document_id,new.normalized_text); END""",
    """CREATE TRIGGER image_ocr_fts_delete AFTER DELETE ON image_ocr_documents BEGIN
        INSERT INTO image_ocr_fts(image_ocr_fts,rowid,normalized_text)
            VALUES('delete',old.document_id,old.normalized_text); END""",
    """CREATE TRIGGER image_feature_plan_immutable
        BEFORE UPDATE OF run_id,work_kind,profile_id,component,digest,plan_json,created_at ON image_feature_runs
        BEGIN SELECT RAISE(ABORT,'Image-feature plans are immutable'); END""",
    """CREATE TRIGGER image_feature_snapshot_immutable
        BEFORE UPDATE OF run_id,item_id,ordinal,photo_id,profile_id,work_kind,input_fingerprint,action,snapshot_json
        ON image_feature_items BEGIN SELECT RAISE(ABORT,'Image-feature snapshots are immutable'); END""",
    """CREATE TRIGGER image_feature_claim_identity BEFORE INSERT ON image_feature_claims
        WHEN NOT EXISTS(SELECT 1 FROM image_feature_items i WHERE i.run_id=new.run_id AND i.item_id=new.item_id
            AND new.scope_key=CASE WHEN i.work_kind='extract' THEN 'photo:'||i.photo_id ELSE i.work_kind END
            AND i.action='compute')
        BEGIN SELECT RAISE(ABORT,'Claim does not match the planned input'); END""",
    """CREATE TRIGGER image_feature_claim_immutable BEFORE UPDATE ON image_feature_claims
        BEGIN SELECT RAISE(ABORT,'Feature claims must be released, not reassigned'); END""",
    """CREATE TRIGGER image_feature_dependency_cycle BEFORE INSERT ON image_feature_dependencies
        WHEN EXISTS(WITH RECURSIVE sources(id) AS (
            SELECT new.source_result_id UNION SELECT d.source_result_id FROM image_feature_dependencies d
            JOIN sources s ON d.result_id=s.id) SELECT 1 FROM sources WHERE id=new.result_id)
        BEGIN SELECT RAISE(ABORT,'Feature dependencies must be acyclic'); END""",
)

_IMMUTABLE_TABLES = (
    "image_feature_profiles", "image_feature_results", "image_feature_dependencies",
    "image_feature_embedding_dependencies", "image_ocr_documents", "image_ocr_blocks",
    "image_object_instances", "image_scene_scores", "image_scene_prototype_sets",
    "image_scene_prototypes", "image_color_features", "image_color_palette",
    "image_composition_features", "image_perceptual_hashes", "image_similarity_pairs",
)
IMAGE_FEATURE_SCHEMA += tuple(
    f"""CREATE TRIGGER {table}_{operation.lower()}_immutable BEFORE {operation} ON {table}
        BEGIN SELECT RAISE(ABORT,'Saved image-feature evidence is immutable'); END"""
    for table in _IMMUTABLE_TABLES for operation in ("UPDATE", "DELETE")
)
IMAGE_FEATURE_SCHEMA += (
    """CREATE TRIGGER image_feature_dependency_identity BEFORE INSERT ON image_feature_dependencies
        WHEN NOT EXISTS(SELECT 1 FROM image_feature_results r
            JOIN image_feature_profiles p ON p.profile_id=r.profile_id
            WHERE r.result_id=new.result_id AND r.photo_id=new.photo_id AND r.content_version=new.content_version
            AND json_extract(r.input_manifest_json,'$.source_result_id')=new.source_result_id
            AND json_extract(r.input_manifest_json,'$.source_profile_id')=new.source_profile_id
            AND json_extract(r.input_manifest_json,'$.source_payload_hash')=new.source_payload_hash
            AND json_extract(p.profile_json,'$.dependencies.objects')=new.source_profile_id)
        BEGIN SELECT RAISE(ABORT,'Feature dependency does not match its source identity'); END""",
    """CREATE TRIGGER image_feature_embedding_identity BEFORE INSERT ON image_feature_embedding_dependencies
        WHEN NOT EXISTS(SELECT 1 FROM image_feature_results r
            JOIN image_feature_profiles p ON p.profile_id=r.profile_id
            JOIN image_embedding_results e ON e.result_id=new.source_result_id
            WHERE r.result_id=new.result_id AND r.photo_id=new.photo_id AND r.content_version=new.content_version
            AND e.vector_hash=new.vector_hash
            AND json_extract(r.input_manifest_json,'$.embedding_result_id')=new.source_result_id
            AND json_extract(r.input_manifest_json,'$.embedding_profile_id')=new.source_profile_id
            AND json_extract(r.input_manifest_json,'$.vector_hash')=new.vector_hash
            AND json_extract(p.profile_json,'$.dependencies.image_embedding')=new.source_profile_id)
        BEGIN SELECT RAISE(ABORT,'Embedding dependency does not match its source identity'); END""",
    """CREATE TRIGGER image_similarity_pair_scope BEFORE INSERT ON image_similarity_pairs
        WHEN NOT EXISTS(SELECT 1 FROM image_feature_runs r WHERE r.run_id=new.run_id
            AND json_extract(r.plan_json,'$.options.metric')=new.metric
            AND new.distance<=coalesce(json_extract(r.plan_json,'$.options.threshold'),
                                       json_extract(r.plan_json,'$.options.max_distance'))
            AND EXISTS(SELECT 1 FROM json_each(r.plan_json,'$.items[0].manifest.participants') a
                WHERE json_extract(a.value,'$.photo_id')=new.photo_id_a
                AND coalesce(json_extract(a.value,'$.content_version'),
                             json_extract(a.value,'$.manifest.content_version'))=new.content_version_a
                AND json_extract(a.value,'$.result_id') IS new.result_id_a)
            AND EXISTS(SELECT 1 FROM json_each(r.plan_json,'$.items[0].manifest.participants') b
                WHERE json_extract(b.value,'$.photo_id')=new.photo_id_b
                AND coalesce(json_extract(b.value,'$.content_version'),
                             json_extract(b.value,'$.manifest.content_version'))=new.content_version_b
                AND json_extract(b.value,'$.result_id') IS new.result_id_b))
        BEGIN SELECT RAISE(ABORT,'Similarity pair is outside its approved comparison scope'); END""",
)
_ITEM_RESULT_CHECK = """WHEN new.result_id IS NOT NULL AND NOT (
    (new.work_kind='extract' AND EXISTS(SELECT 1 FROM image_feature_results r
        WHERE r.result_id=new.result_id AND r.photo_id=new.photo_id AND r.profile_id=new.profile_id
        AND r.input_fingerprint=new.input_fingerprint)) OR
    (new.work_kind='prototypes' AND EXISTS(SELECT 1 FROM image_scene_prototype_sets s
        WHERE s.set_id=new.result_id AND s.profile_id=new.profile_id)))
    BEGIN SELECT RAISE(ABORT,'Job result does not match its immutable input'); END"""
IMAGE_FEATURE_SCHEMA += tuple(
    f"CREATE TRIGGER image_feature_item_result_{operation.lower()} BEFORE {operation} ON image_feature_items "
    + _ITEM_RESULT_CHECK for operation in ("INSERT", "UPDATE")
)


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if json.loads(encoded) != value:
            raise ValueError("Only JSON types are accepted.")
        return encoded
    except (TypeError, ValueError, OverflowError) as exc:
        raise PhotographyError("FEATURE_INVALID", "Feature data must contain finite JSON values.") from exc


def _load(value):
    try:
        result = json.loads(value)
        _json(result)
        return result
    except (ValueError, TypeError) as exc:
        raise PhotographyError("FEATURE_INVALID", "Saved feature JSON is invalid.") from exc


def _require(condition, message, code="FEATURE_INVALID"):
    if not condition:
        raise PhotographyError(code, message)


def _text(value):
    return isinstance(value, str) and bool(value)


def _positive_int(value):
    return type(value) is int and value > 0


def _limit(limit):
    _require(type(limit) is int and 1 <= limit <= 1000, "Limit must be an integer between 1 and 1000.")


@lru_cache(maxsize=1)
def feature_schema_registry():
    # SQLite itself supplies the exact FTS shadow schema for this SQLite version.
    with closing(sqlite3.connect(":memory:")) as reference:
        for statement in IMAGE_FEATURE_SCHEMA:
            reference.execute(statement)
        objects = {(row[0], row[1]): row[2] for row in reference.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*'")}
        columns = {table: {row[1] for row in reference.execute(f'PRAGMA table_info("{table}")')}
                   for table in FEATURE_ALL_TABLES}
    return objects, columns


@contextmanager
def _feature_write(store):
    try:
        with store.transaction():
            before = store.db.total_changes
            yield
            store._record_photo_changes(before)
    except sqlite3.IntegrityError as exc:
        raise PhotographyError("FEATURE_CONFLICT", f"Feature identity or integrity constraint failed: {exc}") from exc


class _ComparisonCheckpoint:
    def __init__(self, store, plan, participants):
        self.store, self.plan, self.participants = store, plan, participants
        self.token = store.change_token()


class ImageFeatureStorage:
    def validate_feature_schema(self):
        from .image_embedding_storage import IMAGE_EMBEDDING_SCHEMA

        expected, columns = feature_schema_registry()
        actual = {(row["type"], row["name"]): row["sql"] for row in self.db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL")
            if row["tbl_name"] in FEATURE_ALL_TABLES or (row["type"], row["name"]) in expected}
        normalize = lambda sql: re.sub(r"\s+", " ", sql.strip())
        _require(set(actual) == set(expected), "Feature schema contains missing or unregistered objects.", "SCHEMA_INVALID")
        for key, sql in expected.items():
            _require(normalize(actual[key]) == normalize(sql),
                     f"Feature schema definition does not match: {key[1]}.", "SCHEMA_INVALID")
        for table, names in columns.items():
            actual_names = {row["name"] for row in self.db.execute(f'PRAGMA table_info("{table}")')}
            _require(actual_names == names, f"Feature table columns do not match: {table}.", "SCHEMA_INVALID")
        triggers = {name: sql for (kind, name), sql in expected.items() if kind == "trigger"}
        for statement in IMAGE_EMBEDDING_SCHEMA:
            match = re.match(r"CREATE TRIGGER IF NOT EXISTS (\w+)", statement)
            if match:
                triggers[match[1]] = statement.replace("CREATE TRIGGER IF NOT EXISTS", "CREATE TRIGGER", 1)
        actual_triggers = {row["name"]: row["sql"] for row in self.db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
        _require(set(triggers) == set(actual_triggers) and all(
            normalize(sql) == normalize(actual_triggers[name]) for name, sql in triggers.items()),
            "Album contains missing, changed or unregistered triggers.", "SCHEMA_INVALID")

    def put_feature_profile(self, profile):
        from .feature_profiles import profile_identity
        profile_id, encoded = profile_identity(profile)
        with _feature_write(self):
            self._profile_dependencies(profile)
            self.db.execute("""INSERT INTO image_feature_profiles VALUES (?,?,?,?,?,?)
                ON CONFLICT(profile_id) DO NOTHING""",
                (profile_id, profile["component"], profile["input_scope"],
                 profile["output_schema_version"], encoded, _timestamp()))
            _require(self.feature_profile(profile_id) == profile, "Saved feature profile differs from its identity.")
        return profile_id

    def _profile_dependencies(self, profile):
        dependencies = profile["dependencies"]
        if profile["input_scope"] == "image_embedding":
            self.embedding_profile(dependencies["image_embedding"])
        if profile["input_scope"] == "object_result":
            source = self.feature_profile(dependencies["objects"])
            _require(source["component"] == "objects", "Composition requires an objects profile.")

    def feature_profile(self, profile_id):
        from .feature_profiles import profile_identity
        row = self.db.execute("SELECT * FROM image_feature_profiles WHERE profile_id=?", (profile_id,)).fetchone()
        _require(row is not None, "Feature profile does not exist.", "FEATURE_PROFILE_NOT_FOUND")
        profile = _load(row["profile_json"])
        _require(profile_identity(profile)[0] == profile_id and
                 all(profile[key] == row[key] for key in ("component", "input_scope", "output_schema_version")),
                 "Saved feature profile failed its identity check.")
        return profile

    def feature_profiles(self, component=None):
        _require(component is None or component in COMPONENT_NAMES, "Unknown feature component.")
        return [{"profile_id": row["profile_id"], "profile": self.feature_profile(row["profile_id"]),
                 "created_at": row["created_at"]} for row in self.db.execute(
                     "SELECT profile_id,created_at FROM image_feature_profiles WHERE (? IS NULL OR component=?) "
                     "ORDER BY created_at,profile_id", (component, component))]

    def default_feature_profile(self, component):
        _require(component in COMPONENT_NAMES, "Unknown feature component.")
        row = self.db.execute("SELECT profile_id FROM image_feature_settings WHERE component=?", (component,)).fetchone()
        if row:
            _require(self.feature_profile(row[0])["component"] == component, "Default feature profile has the wrong component.")
        return row[0] if row else None

    def set_default_feature_profile(self, component, profile_id):
        with _feature_write(self):
            _require(self.feature_profile(profile_id)["component"] == component, "Default profile component does not match.")
            self.db.execute("""INSERT INTO image_feature_settings VALUES (?,?)
                ON CONFLICT(component) DO UPDATE SET profile_id=excluded.profile_id""", (component, profile_id))

    def _manifest(self, profile, manifest, *, allow_partial=False):
        _require(isinstance(manifest, dict), "Input manifest must be an object.")
        manifest = _load(_json(manifest))
        scope = profile["input_scope"]
        fields = {
            "original": {"original_hash", "width", "height"},
            "stored_thumbnail": {"thumbnail_profile", "input_image_hash", "width", "height"},
            "image_embedding": {"embedding_result_id", "embedding_profile_id", "vector_hash",
                                "prototype_set_id", "prototype_hash"},
            "object_result": {"source_result_id", "source_profile_id", "source_payload_hash", "width", "height"},
        }[scope] | {"content_version", "input_scope"}
        _require(({"content_version", "input_scope"} <= set(manifest) <= fields) if allow_partial else set(manifest) == fields,
                 "Input manifest must contain exactly the registered input identity fields.")
        _require(manifest["input_scope"] == scope, "Profile and manifest input scopes differ.")
        for key in manifest:
            _require(_positive_int(manifest[key]) if key in ("width", "height") else _text(manifest[key]),
                     f"Invalid input manifest field: {key}.")
        if scope == "original" and "original_hash" in manifest:
            _require(manifest["original_hash"] == manifest["content_version"], "Original hash differs from ingested content.")
        if scope == "image_embedding" and "embedding_profile_id" in manifest:
            _require(manifest["embedding_profile_id"] == profile["dependencies"]["image_embedding"],
                     "Embedding dependency profile differs from the feature profile.")
        if scope == "object_result" and "source_profile_id" in manifest:
            _require(manifest["source_profile_id"] == profile["dependencies"]["objects"],
                     "Objects dependency profile differs from the feature profile.")
        return manifest

    def _source_evidence(self, photo_id, profile, manifest, feature_ids, embedding_ids, *, current, visiting=None):
        scope = profile["input_scope"]
        _require(isinstance(feature_ids, (list, tuple)) and isinstance(embedding_ids, (list, tuple)),
                 "Dependencies must be lists of result IDs.")
        _require(all(_text(value) for value in (*feature_ids, *embedding_ids)), "Invalid dependency result ID.")
        _require(list(feature_ids) == ([manifest["source_result_id"]] if scope == "object_result" else []),
                 "Feature dependencies do not match the input manifest.")
        _require(list(embedding_ids) == ([manifest["embedding_result_id"]] if scope == "image_embedding" else []),
                 "Embedding dependencies do not match the input manifest.")
        if current:
            photo = self.photo(photo_id)
            _require(photo["content_version"] == manifest["content_version"],
                     "The photo content changed before saving the feature.", "FEATURE_INPUT_CHANGED")
            if scope == "stored_thumbnail":
                thumbnail = self.thumbnail(photo_id, include_data=False)
                _require(photo["thumbnail_profile"] == manifest["thumbnail_profile"] and
                         all(thumbnail[column] == manifest[key] for key, column in (
                    ("content_version", "content_version"), ("thumbnail_profile", "profile"),
                    ("input_image_hash", "image_hash"), ("width", "width"), ("height", "height"))),
                    "Stored thumbnail identity changed.", "FEATURE_INPUT_CHANGED")
        if scope == "object_result":
            source = self._feature_result(manifest["source_result_id"], visiting or set())
            _require(source["component"] == "objects" and source["photo_id"] == photo_id and
                     source["profile_id"] == manifest["source_profile_id"] and
                     source["content_version"] == manifest["content_version"] and
                     source["payload_hash"] == manifest["source_payload_hash"] and
                     all(source["payload"][key] == manifest[key] for key in ("width", "height")),
                     "Objects source does not match this photo and input identity.")
            return source
        if scope == "image_embedding":
            row = self.db.execute("SELECT * FROM image_embedding_results WHERE result_id=?",
                                  (manifest["embedding_result_id"],)).fetchone()
            _require(row is not None, "Embedding source does not exist.")
            source = dict(row)
            _require(source["photo_id"] == photo_id and source["profile_id"] == manifest["embedding_profile_id"] and
                     source["content_version"] == manifest["content_version"] and
                     source["vector_hash"] == manifest["vector_hash"] and source["dtype"] == "float32-le" and
                     source["normalized"] == 1 and hashlib.sha256(source["vector"]).hexdigest() == source["vector_hash"],
                     "Embedding source does not match this photo and vector identity.")
            unpack_vector(source["vector"], source["dimensions"])
            prototypes = self.scene_prototype_set(profile_id=self._profile_id(profile))
            _require(prototypes is not None and prototypes["set_id"] == manifest["prototype_set_id"] and
                     prototypes["payload_hash"] == manifest["prototype_hash"] and
                     all(len(item["vector"]) == source["dimensions"] for item in prototypes["prototypes"]),
                     "Scene prototype identity or dimensions do not match.")
            return source
        return None

    @staticmethod
    def _profile_id(profile):
        from .feature_profiles import profile_identity
        return profile_identity(profile)[0]

    def put_feature_result(self, photo_id, profile_id, input_manifest, payload, *,
                           feature_dependencies=(), embedding_dependencies=()):
        from .feature_profiles import validate_payload
        with _feature_write(self):
            profile = self.feature_profile(profile_id)
            manifest = self._manifest(profile, input_manifest)
            payload = validate_payload(profile, payload)
            encoded = _json(payload)
            _require(all(payload.get(key, manifest.get(key)) == manifest.get(key) for key in ("width", "height")),
                     "Payload dimensions differ from the input manifest.")
            source = self._source_evidence(photo_id, profile, manifest, feature_dependencies, embedding_dependencies,
                                           current=True)
            if profile["component"] == "composition":
                self._composition_source(payload, source)
            identity = fingerprint(manifest)
            old = self.find_feature_result(photo_id, profile_id, identity)
            if old:
                _require(old["payload_hash"] == fingerprint(payload), "A different immutable result already exists.",
                         "FEATURE_CONFLICT")
                return old["result_id"]
            result_id = "feat_" + uuid4().hex
            self.db.execute("""INSERT INTO image_feature_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result_id, photo_id, profile_id, profile["component"], manifest["content_version"],
                 profile["input_scope"], identity, _json(manifest), profile["output_schema_version"],
                 fingerprint(payload), int(payload["complete"]), manifest.get("width"), manifest.get("height"),
                 encoded, _timestamp()))
            self._put_feature_details(result_id, profile["component"], payload)
            if feature_dependencies:
                self.db.execute("""INSERT INTO image_feature_dependencies
                    (result_id,photo_id,source_result_id,source_profile_id,content_version,source_payload_hash)
                    VALUES (?,?,?,?,?,?)""",
                    (result_id, photo_id, source["result_id"], source["profile_id"],
                     source["content_version"], source["payload_hash"]))
            if embedding_dependencies:
                self.db.execute("""INSERT INTO image_feature_embedding_dependencies
                    (result_id,photo_id,source_result_id,source_profile_id,content_version,thumbnail_profile,
                     input_image_hash,vector_hash) VALUES (?,?,?,?,?,?,?,?)""",
                    (result_id, photo_id, source["result_id"], source["profile_id"], source["content_version"],
                     source["thumbnail_profile"], source["input_image_hash"], source["vector_hash"]))
            self.feature_result(result_id)
        return result_id

    def _put_feature_details(self, result_id, component, payload):
        if component == "ocr":
            self.db.execute("""INSERT INTO image_ocr_documents(result_id,text,normalized_text,block_count)
                VALUES (?,?,?,?)""", (result_id, payload["text"], payload["normalized_text"], len(payload["blocks"])))
            self.db.executemany("""INSERT INTO image_ocr_blocks
                (result_id,ordinal,text,polygon_json,recognition_score,detection_score) VALUES (?,?,?,?,?,?)""",
                ((result_id, n, block["text"], _json(block["polygon"]), block["recognition_score"], block["detection_score"])
                 for n, block in enumerate(payload["blocks"])))
        elif component == "objects":
            self.db.executemany("""INSERT INTO image_object_instances
                (result_id,ordinal,class_id,score,x1,y1,x2,y2) VALUES (?,?,?,?,?,?,?,?)""",
                ((result_id, n, item["class_id"], item["score"], *item["bbox"]) for n, item in enumerate(payload["objects"])))
        elif component == "scene":
            self.db.executemany("INSERT INTO image_scene_scores(result_id,ordinal,scene_id,score) VALUES (?,?,?,?)",
                ((result_id, n, item["scene_id"], item["score"]) for n, item in enumerate(payload["scores"])))
        elif component == "color":
            features = payload["features"]
            self.db.execute("""INSERT INTO image_color_features
                (result_id,mean_saturation,low_saturation_fraction,hue_histogram_json) VALUES (?,?,?,?)""",
                (result_id, features["mean_saturation"], features["low_saturation_fraction"], _json(features["hue_histogram"])))
            self.db.executemany("""INSERT INTO image_color_palette
                (result_id,ordinal,red,green,blue,fraction) VALUES (?,?,?,?,?,?)""",
                ((result_id, n, *item["rgb"], item["fraction"]) for n, item in enumerate(payload["palette"])))
        elif component == "composition":
            self.db.execute("""INSERT INTO image_composition_features
                (result_id,subject_index,center_x,center_y,subject_area,thirds_distance,union_area,bounding_area,
                 uncovered_fraction) VALUES (?,?,?,?,?,?,?,?,?)""",
                (result_id, *(payload["features"][key] for key in (
                    "subject_index", "center_x", "center_y", "subject_area", "thirds_distance",
                    "union_area", "bounding_area", "uncovered_fraction"))))
        else:
            self.db.execute("""INSERT INTO image_perceptual_hashes(result_id,algorithm,bits,hash) VALUES (?,?,?,?)""",
                (result_id, payload["algorithm"], payload["bits"], bytes.fromhex(payload["hash_hex"])))

    @staticmethod
    def _composition_source(payload, source):
        _require(payload["complete"] == source["payload"]["complete"], "Composition completeness differs from its object source.")
        subject = payload["features"]["subject_index"]
        _require(subject is None or subject < len(source["payload"]["objects"]),
                 "Composition subject does not exist in its object source.")

    def _detail_rows(self, table, result_id, *, many=False):
        rows = [dict(row) for row in self.db.execute(
            f"SELECT * FROM {table} WHERE result_id=?" + (" ORDER BY ordinal" if many else ""), (result_id,))]
        expected_component = {
            "image_ocr_documents": "ocr", "image_ocr_blocks": "ocr", "image_object_instances": "objects",
            "image_scene_scores": "scene", "image_color_features": "color", "image_color_palette": "color",
            "image_composition_features": "composition", "image_perceptual_hashes": "perceptual_hash",
        }[table]
        _require(all(row["component"] == expected_component for row in rows), "Typed feature detail has the wrong component.")
        if many:
            _require([row["ordinal"] for row in rows] == list(range(len(rows))), "Feature detail ordinals are incomplete.")
            return rows
        _require(len(rows) == 1, "Feature is missing its required typed detail.")
        return rows[0]

    def _reconstruct_payload(self, row):
        result_id, component = row["result_id"], row["component"]
        audit = _load(row["payload_json"])
        _require(isinstance(audit, dict), "Saved payload is not an object.")
        payload = {"complete": bool(row["complete"])}
        if "width" in audit or "height" in audit:
            payload.update(width=row["width"], height=row["height"])
        if component == "ocr":
            document = self._detail_rows("image_ocr_documents", result_id)
            blocks = self._detail_rows("image_ocr_blocks", result_id, many=True)
            _require(document["block_count"] == len(blocks), "OCR block count is corrupt.")
            payload.update(text=document["text"], normalized_text=document["normalized_text"],
                           blocks=[{"text": block["text"], "polygon": _load(block["polygon_json"]),
                                    "recognition_score": block["recognition_score"],
                                    "detection_score": block["detection_score"]} for block in blocks])
        elif component == "objects":
            payload["objects"] = [{"class_id": item["class_id"], "score": item["score"],
                                    "bbox": [item[key] for key in ("x1", "y1", "x2", "y2")]}
                                   for item in self._detail_rows("image_object_instances", result_id, many=True)]
        elif component == "scene":
            payload["scores"] = [{"scene_id": item["scene_id"], "score": item["score"]}
                                 for item in self._detail_rows("image_scene_scores", result_id, many=True)]
        elif component == "color":
            detail = self._detail_rows("image_color_features", result_id)
            payload["features"] = {"mean_saturation": detail["mean_saturation"],
                                   "low_saturation_fraction": detail["low_saturation_fraction"],
                                   "hue_histogram": _load(detail["hue_histogram_json"])}
            payload["palette"] = [{"rgb": [item["red"], item["green"], item["blue"]], "fraction": item["fraction"]}
                                  for item in self._detail_rows("image_color_palette", result_id, many=True)]
        elif component == "composition":
            detail = self._detail_rows("image_composition_features", result_id)
            payload["features"] = {key: value for key, value in detail.items() if key not in ("result_id", "component")}
        else:
            detail = self._detail_rows("image_perceptual_hashes", result_id)
            payload.update(algorithm=detail["algorithm"], bits=detail["bits"], hash_hex=detail["hash"].hex())
        _require(payload == audit, "Typed feature data differs from the saved canonical payload.")
        return self._payload_number_types(payload, audit)

    @staticmethod
    def _payload_number_types(value, audit):
        # SQLite REAL affinity loses JSON's 1 versus 1.0 spelling. The audit may
        # restore numeric representation only after all typed values compare equal.
        if isinstance(value, dict):
            return {key: ImageFeatureStorage._payload_number_types(item, audit[key]) for key, item in value.items()}
        if isinstance(value, list):
            return [ImageFeatureStorage._payload_number_types(item, original) for item, original in zip(value, audit)]
        if type(value) in (int, float) and type(audit) in (int, float):
            return audit
        return value

    def _feature_result(self, result_id, visiting):
        from .feature_profiles import validate_payload
        _require(result_id not in visiting, "Saved feature dependencies contain a cycle.")
        visiting = visiting | {result_id}
        row = self.db.execute("SELECT * FROM image_feature_results WHERE result_id=?", (result_id,)).fetchone()
        _require(row is not None, "Feature result does not exist.", "FEATURE_RESULT_NOT_FOUND")
        profile = self.feature_profile(row["profile_id"])
        _require(type(row["complete"]) is int and row["complete"] in (0, 1), "Saved feature completeness is invalid.")
        _require(all(row[key] == profile[key] for key in ("component", "input_scope", "output_schema_version")),
                 "Feature result profile or component identity is corrupt.")
        manifest = self._manifest(profile, _load(row["input_manifest_json"]))
        _require(fingerprint(manifest) == row["input_fingerprint"] and manifest["content_version"] == row["content_version"]
                 and all(row[key] == manifest.get(key) for key in ("width", "height")), "Feature input identity is corrupt.")
        payload = validate_payload(profile, self._reconstruct_payload(row))
        _require(fingerprint(payload) == row["payload_hash"], "Saved feature payload failed its hash check.")
        feature_ids = [entry[0] for entry in self.db.execute(
            "SELECT source_result_id FROM image_feature_dependencies WHERE result_id=? ORDER BY source_result_id", (result_id,))]
        embedding_ids = [entry[0] for entry in self.db.execute(
            "SELECT source_result_id FROM image_feature_embedding_dependencies WHERE result_id=?", (result_id,))]
        source = self._source_evidence(row["photo_id"], profile, manifest, feature_ids, embedding_ids,
                                       current=False, visiting=visiting)
        if row["component"] == "composition":
            self._composition_source(payload, source)
        if feature_ids:
            dependency = self.db.execute("SELECT * FROM image_feature_dependencies WHERE result_id=?", (result_id,)).fetchone()
            _require(dependency["photo_id"] == row["photo_id"] and dependency["component"] == row["component"] and
                     dependency["source_component"] == source["component"] and
                     dependency["source_profile_id"] == source["profile_id"] and
                     dependency["content_version"] == source["content_version"] and
                     dependency["source_payload_hash"] == source["payload_hash"], "Saved feature dependency is corrupt.")
        if embedding_ids:
            dependency = self.db.execute("SELECT * FROM image_feature_embedding_dependencies WHERE result_id=?", (result_id,)).fetchone()
            _require(dependency["photo_id"] == row["photo_id"] and dependency["component"] == row["component"] and
                     dependency["source_profile_id"] == source["profile_id"] and
                     all(dependency[key] == source[key] for key in (
                         "content_version", "thumbnail_profile", "input_image_hash", "vector_hash")),
                     "Saved embedding dependency is corrupt.")
        return {**{key: row[key] for key in ("result_id", "photo_id", "profile_id", "component", "content_version",
                    "input_scope", "input_fingerprint", "payload_hash", "created_at")},
                "input_manifest": manifest, "payload": payload,
                "feature_dependencies": feature_ids, "embedding_dependencies": embedding_ids}

    def feature_result(self, result_id):
        with self.read_snapshot():
            try:
                return self._feature_result(result_id, set())
            except PhotographyError as exc:
                raise PhotographyError("FEATURE_RESULT_INVALID", str(exc)) from exc
            except (TypeError, ValueError, KeyError, AttributeError, OverflowError, sqlite3.DatabaseError) as exc:
                raise PhotographyError("FEATURE_RESULT_INVALID", "Saved feature result is not reconstructible.") from exc

    def find_feature_result(self, photo_id, profile_id, input_fingerprint):
        with self.read_snapshot():
            row = self.db.execute("""SELECT result_id FROM image_feature_results
                WHERE photo_id=? AND profile_id=? AND input_fingerprint=?""",
                (photo_id, profile_id, input_fingerprint)).fetchone()
            return self.feature_result(row[0]) if row else None

    def has_feature_results(self, photo_id, profile_id):
        return self.db.execute("SELECT 1 FROM image_feature_results WHERE photo_id=? AND profile_id=? LIMIT 1",
                               (photo_id, profile_id)).fetchone() is not None

    def feature_history(self, photo_id, profile_id, *, limit=100, after=""):
        _limit(limit)
        _require(isinstance(after, str), "History cursor must be a result ID.")
        with self.read_snapshot():
            return [self.feature_result(row[0]) for row in self.db.execute("""SELECT result_id FROM image_feature_results
                WHERE photo_id=? AND profile_id=? AND result_id>? ORDER BY result_id LIMIT ?""",
                (photo_id, profile_id, after, limit))]

    def rebuild_feature_fts(self):
        with _feature_write(self):
            self.validate_feature_schema()
            result_ids = [row[0] for row in self.db.execute(
                "SELECT result_id FROM image_feature_results WHERE component='ocr' ORDER BY result_id")]
            for result_id in result_ids:
                self.feature_result(result_id)
            self.db.execute("INSERT INTO image_ocr_fts(image_ocr_fts) VALUES('rebuild')")
            self.db.execute("INSERT INTO image_ocr_fts(image_ocr_fts,rank) VALUES('integrity-check',1)")
        return {"documents": len(result_ids), "rebuilt": True}

    def _prepare_prototypes(self, profile, prototypes):
        from .feature_algorithms import validate_scene_prototypes

        self._profile_dependencies(profile)
        _require(profile["component"] == "scene", "Prototype sets require a scene profile.")
        embedding = self.embedding_profile(profile["dependencies"]["image_embedding"])
        dimensions = embedding.get("dimensions")
        _require(_positive_int(dimensions) and dimensions <= 4096, "Embedding profile must declare its vector dimensions.")
        prepared = []
        for item in validate_scene_prototypes(profile, prototypes, dimensions=dimensions):
            blob = pack_vector(item["vector"], dimensions)
            prepared.append({"scene_id": item["scene_id"], "label": item["label"],
                             "prompts": item["prompts"], "vector": unpack_vector(blob, dimensions)})
        return prepared

    def put_scene_prototypes(self, profile_id, prototypes):
        with _feature_write(self):
            profile = self.feature_profile(profile_id)
            prepared = self._prepare_prototypes(profile, prototypes)
            payload_hash = fingerprint(prepared)
            old = self.scene_prototype_set(profile_id)
            if old:
                _require(old["payload_hash"] == payload_hash, "This scene profile already has an immutable prototype set.",
                         "FEATURE_CONFLICT")
                return old["set_id"]
            set_id = "prototypes_" + uuid4().hex
            self.db.execute("""INSERT INTO image_scene_prototype_sets
                (set_id,profile_id,embedding_profile_id,payload_hash,created_at) VALUES (?,?,?,?,?)""",
                (set_id, profile_id, profile["dependencies"]["image_embedding"], payload_hash, _timestamp()))
            for ordinal, item in enumerate(prepared):
                blob = pack_vector(item["vector"], len(item["vector"]))
                self.db.execute("INSERT INTO image_scene_prototypes VALUES (?,?,?,?,?,?,?,?)",
                    (set_id, ordinal, item["scene_id"], item["label"], _json(item["prompts"]), blob,
                     hashlib.sha256(blob).hexdigest(), len(item["vector"])))
            self.scene_prototype_set(profile_id)
        return set_id

    def scene_prototype_set(self, profile_id):
        try:
            return self._scene_prototype_set(profile_id)
        except PhotographyError as exc:
            if exc.code in ("FEATURE_PROFILE_NOT_FOUND", "FEATURE_PROFILE_INVALID"):
                raise
            raise PhotographyError("FEATURE_RESULT_INVALID", str(exc)) from exc
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError, sqlite3.DatabaseError) as exc:
            raise PhotographyError("FEATURE_RESULT_INVALID", "Saved prototype set is not reconstructible.") from exc

    def _scene_prototype_set(self, profile_id):
        with self.read_snapshot():
            profile = self.feature_profile(profile_id)
            _require(profile["component"] == "scene", "Prototype sets require a scene profile.")
            row = self.db.execute("SELECT * FROM image_scene_prototype_sets WHERE profile_id=?", (profile_id,)).fetchone()
            if row is None:
                return None
            _require(row["component"] == "scene" and row["embedding_profile_id"] == profile["dependencies"]["image_embedding"],
                     "Saved prototype profile identity is corrupt.")
            prototypes = []
            for item in self.db.execute("SELECT * FROM image_scene_prototypes WHERE set_id=? ORDER BY ordinal", (row["set_id"],)):
                _require(item["ordinal"] == len(prototypes) and
                         hashlib.sha256(item["vector"]).hexdigest() == item["vector_hash"],
                         "Saved scene prototype vector or ordinal is corrupt.")
                prototypes.append({"scene_id": item["scene_id"], "label": item["label"],
                                   "prompts": _load(item["prompts_json"]),
                                   "vector": unpack_vector(item["vector"], item["dimensions"])})
            prepared = self._prepare_prototypes(profile, prototypes)
            _require(fingerprint(prepared) == row["payload_hash"], "Saved scene prototype set failed its hash check.")
            return {"set_id": row["set_id"], "profile_id": profile_id, "prototypes": prepared,
                    "payload_hash": row["payload_hash"]}

    def _validate_prototype_manifest(self, profile, manifest):
        _require(set(manifest) == {"embedding_profile_id", "embedding_profile_hash", "catalog"},
                 "Prototype input must freeze its embedding profile and complete catalog.")
        dependency = profile["dependencies"]["image_embedding"]
        embedding = self.embedding_profile(dependency)
        _require(manifest["embedding_profile_id"] == dependency and
                 manifest["embedding_profile_hash"] == fingerprint(embedding) and
                 manifest["catalog"] == profile["parameters"]["catalog"], "Prototype input identity differs from its profile.")

    def _validate_feature_plan(self, plan):
        _require(isinstance(plan, dict), "Feature plan must be an object.", "FEATURE_PLAN_INVALID")
        plan = _load(_json(plan))
        fields = {"schema", "run_id", "work_kind", "album_id", "component", "profile_id", "profile",
                  "created_at", "options", "items", "counts", "digest"}
        _require(fields <= set(plan) <= fields | {"status"}, "Invalid feature plan fields.", "FEATURE_PLAN_INVALID")
        _require(plan["schema"] == "image-feature-plan-v1" and _text(plan["run_id"]) and
                 plan["album_id"] == self.album()["id"], "Feature plan belongs to a different album or schema.",
                 "FEATURE_PLAN_INVALID")
        profile = self.feature_profile(plan["profile_id"])
        _require(plan["profile"] == profile and plan["component"] == profile["component"],
                 "Feature plan profile identity does not match.", "FEATURE_PLAN_INVALID")
        _require(plan["digest"] == fingerprint({key: value for key, value in plan.items() if key != "digest"}),
                 "Feature plan digest does not match its immutable contents.", "FEATURE_PLAN_INVALID")
        _require(plan["work_kind"] in ("extract", "prototypes", "compare") and isinstance(plan["items"], list) and
                 isinstance(plan["options"], dict) and isinstance(plan["counts"], dict),
                 "Feature plan has invalid scope fields.", "FEATURE_PLAN_INVALID")
        _require(_text(plan["created_at"]), "Feature plan needs a creation timestamp.", "FEATURE_PLAN_INVALID")
        try:
            datetime.fromisoformat(plan["created_at"])
        except ValueError as exc:
            raise PhotographyError("FEATURE_PLAN_INVALID", "Feature plan creation timestamp is invalid.") from exc
        if plan["work_kind"] == "prototypes":
            _require(profile["component"] == "scene" and len(plan["items"]) == 1, "Prototype run requires one scene scope item.")
        if plan["work_kind"] == "compare":
            _require(profile["component"] == "perceptual_hash" and len(plan["items"]) == 1,
                     "Compare run requires one perceptual-hash scope item.")
            self._compare_options(plan)
        item_ids = set()
        for item in plan["items"]:
            item_fields = {"item_id", "photo_id", "manifest", "input_fingerprint", "action", "result_id", "reason"}
            _require(isinstance(item, dict) and item_fields <= set(item) <= item_fields | {"error"},
                     "Invalid feature plan item fields.", "FEATURE_PLAN_INVALID")
            _require(_text(item["item_id"]) and item["item_id"] not in item_ids and
                     item["action"] in ("compute", "reuse", "skip") and isinstance(item["manifest"], dict),
                     "Invalid or duplicate feature plan item.", "FEATURE_PLAN_INVALID")
            item_ids.add(item["item_id"])
            _require(item["input_fingerprint"] == fingerprint(item["manifest"]),
                     "Planned input fingerprint does not match its manifest.", "FEATURE_PLAN_INVALID")
            _require(item["result_id"] is None or _text(item["result_id"]), "Invalid planned result ID.")
            _require(item["reason"] is None or isinstance(item["reason"], str), "Invalid planned reason.")
            _require("error" not in item or item["error"] is None or isinstance(item["error"], dict), "Invalid planned error.")
            if plan["work_kind"] == "extract":
                _require(_text(item["photo_id"]), "Extraction items require a photo.")
                self.photo(item["photo_id"])
                self._manifest(profile, item["manifest"], allow_partial=item["action"] == "skip")
            else:
                _require(item["photo_id"] is None, "Prototype and comparison jobs must not impersonate a photo.")
                if plan["work_kind"] == "prototypes":
                    self._validate_prototype_manifest(profile, item["manifest"])
            if item["action"] == "reuse":
                _require(item["result_id"] is not None, "Reuse items require an exact saved result.")
            if item["result_id"] is not None:
                self._check_item_result(plan, item, item["result_id"])
        if plan["work_kind"] == "compare":
            self._compare_participants(plan)
        expected_counts = {"total": len(plan["items"]), **{
            action: sum(item["action"] == action for item in plan["items"]) for action in ("compute", "reuse", "skip")}}
        _require(plan["counts"] == expected_counts, "Feature plan counts differ from its immutable item scope.")
        return plan

    def _check_item_result(self, plan, snapshot, result_id):
        if result_id is None:
            return
        if plan["work_kind"] == "extract":
            result = self.feature_result(result_id)
            _require(result["photo_id"] == snapshot["photo_id"] and result["profile_id"] == plan["profile_id"] and
                     result["input_fingerprint"] == snapshot["input_fingerprint"],
                     "Job result does not match its photo, profile and frozen input.")
        elif plan["work_kind"] == "prototypes":
            prototypes = self.scene_prototype_set(plan["profile_id"])
            _require(prototypes is not None and prototypes["set_id"] == result_id,
                     "Prototype job result does not match its scene profile.")
        else:
            _require(False, "Comparison items cannot reference feature or prototype results.")

    def create_feature_run(self, plan):
        with _feature_write(self):
            plan = self._validate_feature_plan(plan)
            stamp = _timestamp()
            self.db.execute("""INSERT INTO image_feature_runs
                (run_id,work_kind,profile_id,component,digest,plan_json,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (plan["run_id"], plan["work_kind"], plan["profile_id"], plan["component"], plan["digest"],
                 _json(plan), plan.get("status", "proposed"), plan["created_at"], stamp))
            for ordinal, item in enumerate(plan["items"]):
                self.db.execute("""INSERT INTO image_feature_items
                    (run_id,item_id,ordinal,photo_id,profile_id,work_kind,input_fingerprint,action,snapshot_json,status,
                     attempts_json,error_json,result_id,progress_json,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,'pending','[]',NULL,NULL,'{}',?)""",
                    (plan["run_id"], item["item_id"], ordinal, item["photo_id"], plan["profile_id"], plan["work_kind"],
                     item["input_fingerprint"], item["action"], _json(item), stamp))

    def feature_run(self, run_id):
        with self.read_snapshot():
            row = self.db.execute("SELECT * FROM image_feature_runs WHERE run_id=?", (run_id,)).fetchone()
            _require(row is not None, "Feature run does not exist.", "FEATURE_RUN_NOT_FOUND")
            plan = _load(row["plan_json"])
            _require(isinstance(plan, dict) and plan.get("digest") == row["digest"] and
                     fingerprint({key: value for key, value in plan.items() if key != "digest"}) == row["digest"] and
                     all(plan.get(key) == row[key] for key in ("run_id", "work_kind", "profile_id", "component", "created_at")) and
                     plan.get("album_id") == self.album()["id"] and plan.get("profile") == self.feature_profile(row["profile_id"]),
                     "Saved feature plan failed its identity check.", "FEATURE_PLAN_INVALID")
            _require(row["confirmed_digest"] in (None, row["digest"]) and row["model_calls"] >= 0,
                     "Saved feature progress is invalid.", "FEATURE_PLAN_INVALID")
            return {"plan": plan, **{key: row[key] for key in
                    ("status", "confirmed_digest", "confirmed_at", "model_calls", "updated_at")}}

    def feature_items(self, run_id):
        with self.read_snapshot():
            plan = self.feature_run(run_id)["plan"]
            rows = self.db.execute("SELECT * FROM image_feature_items WHERE run_id=? ORDER BY ordinal", (run_id,)).fetchall()
            _require(len(rows) == len(plan["items"]), "Saved job items are incomplete.", "FEATURE_PLAN_INVALID")
            items = []
            for ordinal, row in enumerate(rows):
                item = self._feature_item_from_row(plan, row)
                _require(row["ordinal"] == ordinal and item["snapshot"] == plan["items"][ordinal],
                         "Saved feature item identity is corrupt.", "FEATURE_PLAN_INVALID")
                items.append(item)
            return items

    def _feature_run_metadata(self, run_id):
        # Plans and item identities are immutable SQL records. Hot progress writes
        # use their keys; public job reads still verify the complete plan digest.
        row = self.db.execute("""SELECT r.run_id,r.work_kind,r.profile_id,r.component,r.digest,
            r.confirmed_digest,r.model_calls,p.component AS profile_component
            FROM image_feature_runs r LEFT JOIN image_feature_profiles p ON p.profile_id=r.profile_id
            WHERE r.run_id=?""", (run_id,)).fetchone()
        _require(row is not None, "Feature run does not exist.", "FEATURE_RUN_NOT_FOUND")
        _require(row["work_kind"] in ("extract", "prototypes", "compare") and
                 row["component"] == row["profile_component"] and
                 row["confirmed_digest"] in (None, row["digest"]) and
                 type(row["model_calls"]) is int and row["model_calls"] >= 0,
                 "Saved feature run identity or progress is corrupt.", "FEATURE_PLAN_INVALID")
        return dict(row)

    def _feature_item_from_row(self, run, row):
        snapshot = _load(row["snapshot_json"])
        _require(isinstance(snapshot, dict) and
                 all(row[key] == snapshot.get(key) for key in ("item_id", "photo_id", "input_fingerprint", "action")) and
                 row["profile_id"] == run["profile_id"] and row["work_kind"] == run["work_kind"] and
                 row["run_id"] == run["run_id"] and row["action"] in ("compute", "reuse", "skip") and
                 isinstance(snapshot.get("manifest"), dict) and
                 fingerprint(snapshot["manifest"]) == row["input_fingerprint"] and
                 (_text(row["photo_id"]) if run["work_kind"] == "extract" else row["photo_id"] is None),
                 "Saved feature item identity is corrupt.", "FEATURE_PLAN_INVALID")
        item = {"item_id": row["item_id"], "photo_id": row["photo_id"], "snapshot": snapshot,
                "status": row["status"], "attempts": _load(row["attempts_json"]),
                "error": _load(row["error_json"]) if row["error_json"] is not None else None,
                "result_id": row["result_id"], "progress": _load(row["progress_json"])}
        _require(isinstance(item["attempts"], list) and isinstance(item["progress"], dict) and
                 item["status"] in FEATURE_ITEM_STATES and
                 (item["error"] is None or isinstance(item["error"], dict)), "Saved feature job progress is corrupt.")
        self._validate_item_progress(run["work_kind"], snapshot, item["progress"])
        self._check_item_result(run, snapshot, item["result_id"])
        return item

    def _target_feature_item(self, run_id, item_id):
        run = self._feature_run_metadata(run_id)
        row = self.db.execute("SELECT * FROM image_feature_items WHERE run_id=? AND item_id=?",
                              (run_id, item_id)).fetchone()
        _require(row is not None, "Feature run item does not exist.", "FEATURE_ITEM_NOT_FOUND")
        return run, self._feature_item_from_row(run, row)

    def update_feature_run(self, run_id, **changes):
        _require(set(changes) <= {"status", "confirmed_digest", "confirmed_at", "model_calls"},
                 "Only mutable feature run progress can be updated.")
        with _feature_write(self):
            current = self._feature_run_metadata(run_id)
            if "status" in changes:
                _require(_text(changes["status"]), "Run status must be a nonempty string.")
            if "confirmed_digest" in changes:
                _require(changes["confirmed_digest"] in (None, current["digest"]), "Run confirmation digest does not match.")
            if "model_calls" in changes:
                _require(type(changes["model_calls"]) is int and changes["model_calls"] >= current["model_calls"],
                         "Model call count must be a nondecreasing integer.")
            if "confirmed_at" in changes:
                _require(changes["confirmed_at"] is None or _text(changes["confirmed_at"]), "Invalid confirmation timestamp.")
            values = {**changes, "updated_at": _timestamp()}
            self.db.execute("UPDATE image_feature_runs SET " + ",".join(key + "=?" for key in values) + " WHERE run_id=?",
                            (*values.values(), run_id))

    def update_feature_item(self, run_id, item):
        _require(isinstance(item, dict) and _text(item.get("item_id")), "Item update requires an item ID.")
        with _feature_write(self):
            run, current = self._target_feature_item(run_id, item["item_id"])
            _require(set(item) <= set(current), "Unknown feature item fields.")
            for key in ("photo_id", "snapshot"):
                _require(key not in item or item[key] == current[key], "The planned feature input is immutable.")
            updated = {**current, **item}
            _require(updated["status"] in FEATURE_ITEM_STATES and isinstance(updated["attempts"], list) and
                     isinstance(updated["progress"], dict) and (updated["error"] is None or isinstance(updated["error"], dict)),
                     "Invalid feature item progress.")
            self._validate_item_progress(run["work_kind"], current["snapshot"], updated["progress"])
            if updated["result_id"] != current["result_id"]:
                self._check_item_result(run, current["snapshot"], updated["result_id"])
            if updated["status"] in ("computed", "cached") and run["work_kind"] != "compare":
                _require(updated["result_id"] is not None, "Successful feature jobs require a saved result.")
            self.db.execute("""UPDATE image_feature_items SET status=?,attempts_json=?,error_json=?,result_id=?,
                progress_json=?,updated_at=? WHERE run_id=? AND item_id=?""",
                (updated["status"], _json(updated["attempts"]),
                 _json(updated["error"]) if updated["error"] is not None else None,
                 updated["result_id"], _json(updated["progress"]), _timestamp(), run_id, item["item_id"]))

    @staticmethod
    def _validate_item_progress(work_kind, snapshot, progress):
        if work_kind != "compare":
            return
        _require(set(progress) <= {"comparisons"}, "Comparison progress must contain only its comparison cursor.")
        if "comparisons" in progress:
            participants = snapshot["manifest"].get("participants")
            _require(isinstance(participants, list), "Comparison input is missing its participant scope.")
            maximum = len(participants) * (len(participants) - 1) // 2
            _require(type(progress["comparisons"]) is int and 0 <= progress["comparisons"] <= maximum,
                     "Comparison progress is outside the approved pair scope.")

    def claim_feature_input(self, run_id, item_id, profile_id, input_fingerprint):
        with _feature_write(self):
            run, item = self._target_feature_item(run_id, item_id)
            _require(item["snapshot"]["action"] == "compute" and
                     profile_id == run["profile_id"] and input_fingerprint == item["snapshot"]["input_fingerprint"],
                     "Claim does not match the exact planned run, item, profile and input.")
            scope = "photo:" + item["photo_id"] if item["photo_id"] is not None else run["work_kind"]
            existing = self.db.execute("""SELECT run_id,item_id FROM image_feature_claims
                WHERE profile_id=? AND input_fingerprint=? AND scope_key=?""", (profile_id, input_fingerprint, scope)).fetchone()
            _require(existing is None, "This feature input is already claimed.", "FEATURE_IN_PROGRESS")
            self.db.execute("INSERT INTO image_feature_claims VALUES (?,?,?,?,?,?)",
                (profile_id, input_fingerprint, scope, run_id, item_id, _timestamp()))

    def release_feature_input(self, run_id, item_id):
        with _feature_write(self):
            self._feature_run_metadata(run_id)
            self.db.execute("DELETE FROM image_feature_claims WHERE run_id=? AND item_id=?", (run_id, item_id))

    @staticmethod
    def _compare_options(plan):
        options = plan["options"]
        metric = options.get("metric")
        threshold = options.get("threshold", options.get("max_distance"))
        _require("threshold" not in options or "max_distance" not in options or options["threshold"] == options["max_distance"],
                 "Comparison threshold options disagree.")
        _require(metric in ("exact", "hamming"), "Comparison metric must be exact or hamming.")
        _require(type(threshold) in (int, float) and math.isfinite(threshold) and threshold >= 0,
                 "Comparison threshold must be finite and nonnegative.")
        _require((metric == "exact" and threshold == 0) or (metric == "hamming" and threshold <= 64),
                 "Comparison threshold does not match its metric.")
        return metric, threshold

    def _compare_participants(self, plan):
        _require(plan["work_kind"] == "compare" and len(plan["items"]) == 1 and plan["items"][0]["photo_id"] is None,
                 "Comparison requires one non-photo scope item.")
        scope_manifest = plan["items"][0]["manifest"]
        _require(isinstance(scope_manifest, dict) and "participants" in scope_manifest and
                 set(scope_manifest) <= {"participants", "metric", "threshold", "max_distance"},
                 "Unknown comparison scope manifest fields.")
        participants = scope_manifest.get("participants")
        _require(isinstance(participants, list) and len(participants) >= 2,
                 "Comparison scope manifest requires at least two participants.")
        maximum = len(participants) * (len(participants) - 1) // 2
        _require(plan["options"].get("comparison_count", maximum) == maximum and
                 type(plan["options"].get("comparison_count", maximum)) is int,
                 "Comparison count differs from the approved participant scope.")
        result = {}
        metric, unused = self._compare_options(plan)
        _require(scope_manifest.get("metric", metric) == metric and
                 scope_manifest.get("threshold", scope_manifest.get("max_distance", unused)) == unused,
                 "Comparison manifest and approved metric/threshold disagree.")
        for participant in participants:
            _require(isinstance(participant, dict) and _text(participant.get("photo_id")) and
                     participant["photo_id"] not in result, "Comparison participants must be unique photos.")
            _require(set(participant) <= {"photo_id", "content_version", "result_id", "input_fingerprint",
                                          "payload_hash", "manifest"}, "Unknown comparison participant fields.")
            manifest = participant.get("manifest", participant)
            _require(isinstance(manifest, dict), "Comparison participant manifest must be an object.")
            content_version = participant.get("content_version", manifest.get("content_version"))
            result_id = participant.get("result_id")
            _require(_text(content_version), "Comparison participants require frozen photo content versions.")
            if metric == "exact":
                _require(result_id is None, "Exact comparisons must use photo content identity, not feature results.")
            else:
                _require(_text(result_id), "Hash comparisons require exact saved feature results.")
                source = self.feature_result(result_id)
                _require(source["component"] == "perceptual_hash" and source["photo_id"] == participant["photo_id"] and
                         source["profile_id"] == plan["profile_id"] and source["content_version"] == content_version,
                         "Comparison participant result does not match the planned photo and profile.")
                if "input_fingerprint" in participant:
                    _require(source["input_fingerprint"] == participant["input_fingerprint"],
                             "Comparison participant input fingerprint does not match.")
                if "payload_hash" in participant:
                    _require(source["payload_hash"] == participant["payload_hash"], "Comparison participant payload hash differs.")
                if "manifest" in participant:
                    _require(source["input_manifest"] == manifest, "Comparison participant manifest does not match.")
            result[participant["photo_id"]] = {"content_version": content_version, "result_id": result_id}
            if metric == "hamming":
                result[participant["photo_id"]].update(input_fingerprint=source["input_fingerprint"],
                    payload_hash=source["payload_hash"], manifest=source["input_manifest"])
        return result

    def _comparison_source(self, plan, photo_id, participant):
        source = self.feature_result(participant["result_id"])
        _require(source["photo_id"] == photo_id and source["profile_id"] == plan["profile_id"] and
                 source["component"] == "perceptual_hash" and
                 source["content_version"] == participant["content_version"] and
                 source["input_fingerprint"] == participant["input_fingerprint"] and
                 source["payload_hash"] == participant["payload_hash"] and
                 source["input_manifest"] == participant["manifest"],
                 "Comparison source differs from its frozen identity.", "FEATURE_INPUT_CHANGED")
        return source

    def _validate_pair(self, plan, participants, pair, *, current, verified_payloads=None):
        fields = {"photo_id_a", "photo_id_b", "result_id_a", "result_id_b",
                  "content_version_a", "content_version_b", "metric", "distance"}
        _require(isinstance(pair, dict) and set(pair) == fields, "Invalid similarity pair fields.")
        pair = _load(_json(pair))
        _require(_text(pair["photo_id_a"]) and _text(pair["photo_id_b"]) and pair["photo_id_a"] != pair["photo_id_b"],
                 "Similarity pairs must contain two distinct photos.")
        if pair["photo_id_a"] > pair["photo_id_b"]:
            for key in ("photo_id", "result_id", "content_version"):
                pair[key + "_a"], pair[key + "_b"] = pair[key + "_b"], pair[key + "_a"]
        metric, threshold = self._compare_options(plan)
        distance = pair["distance"]
        _require(pair["metric"] == metric and type(distance) in (int, float) and math.isfinite(distance) and
                 0 <= distance <= threshold, "Pair distance or metric falls outside the approved comparison.")
        sources = []
        for suffix in ("a", "b"):
            photo_id = pair["photo_id_" + suffix]
            participant = participants.get(photo_id)
            _require(participant is not None and
                     participant["content_version"] == pair["content_version_" + suffix] and
                     participant["result_id"] == pair["result_id_" + suffix], "Pair endpoint is outside its planned scope.")
            if current:
                _require(self.photo(photo_id)["content_version"] == participant["content_version"],
                         "Comparison photo content changed.", "FEATURE_INPUT_CHANGED")
            if metric == "hamming":
                sources.append(verified_payloads[photo_id] if verified_payloads is not None else
                               self._comparison_source(plan, photo_id, participant)["payload"])
        if metric == "exact":
            _require(pair["result_id_a"] is None and pair["result_id_b"] is None and distance == 0 and
                     pair["content_version_a"] == pair["content_version_b"], "Exact pair identities are not equal.")
        else:
            from .feature_algorithms import hash_distance

            _require(distance == hash_distance(sources[0], sources[1]),
                     "Saved pair distance is not the distance of its source results.")
        return pair

    def put_similarity_pairs(self, run_id, pairs):
        _require(isinstance(pairs, (list, tuple)), "Similarity pairs must be a list.")
        with _feature_write(self):
            plan = self.feature_run(run_id)["plan"]
            participants = self._compare_participants(plan)
            self._put_similarity_pairs(plan, participants, pairs)

    def _put_similarity_pairs(self, plan, participants, pairs, *, verified_payloads=None):
        run_id = plan["run_id"]
        for supplied in pairs:
            pair = self._validate_pair(plan, participants, supplied, current=verified_payloads is None,
                                       verified_payloads=verified_payloads)
            existing = self.db.execute("""SELECT * FROM image_similarity_pairs
                WHERE run_id=? AND photo_id_a=? AND photo_id_b=? AND metric=?""",
                (run_id, pair["photo_id_a"], pair["photo_id_b"], pair["metric"])).fetchone()
            if existing is not None:
                _require(all(existing[key] == value for key, value in pair.items()),
                         "A different immutable pair already exists.", "FEATURE_CONFLICT")
                continue
            self.db.execute("""INSERT INTO image_similarity_pairs
                (run_id,profile_id,photo_id_a,photo_id_b,result_id_a,result_id_b,content_version_a,content_version_b,
                 metric,distance) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (run_id, plan["profile_id"], *(pair[key] for key in (
                    "photo_id_a", "photo_id_b", "result_id_a", "result_id_b", "content_version_a", "content_version_b",
                    "metric", "distance"))))

    def prepare_similarity_checkpoint(self, run_id):
        with self.read_snapshot():
            plan = self._validate_feature_plan(self.feature_run(run_id)["plan"])
            participants = self._compare_participants(plan)
            items = self.feature_items(run_id)
            _require(len(items) == 1 and items[0]["snapshot"] == plan["items"][0],
                     "Comparison checkpoint scope changed.", "FEATURE_PLAN_INVALID")
            return _ComparisonCheckpoint(self, plan, participants)

    def put_similarity_checkpoint(self, checkpoint, pairs, comparisons, photo_ids):
        """Commit a bounded batch and its cursor without rereading the frozen scope."""
        _require(isinstance(checkpoint, _ComparisonCheckpoint) and checkpoint.store is self,
                 "Comparison checkpoint belongs to another storage connection.")
        _require(isinstance(pairs, (list, tuple)) and isinstance(photo_ids, (set, list, tuple)),
                 "Comparison checkpoint requires pairs and their processed endpoints.")
        with _feature_write(self):
            plan = checkpoint.plan
            if checkpoint.token != self.change_token():
                # Other writes (including trigger/plan tampering) never inherit a
                # cached approval. Only our own pair/cursor writes advance the token.
                refreshed = self.prepare_similarity_checkpoint(plan["run_id"])
                _require(refreshed.plan == plan and refreshed.participants == checkpoint.participants,
                         "Comparison checkpoint identity changed.", "FEATURE_PLAN_INVALID")
            snapshot = plan["items"][0]
            run = self._feature_run_metadata(plan["run_id"])
            row = self.db.execute("""SELECT photo_id,profile_id,work_kind,input_fingerprint,action,
                status,result_id,progress_json FROM image_feature_items WHERE run_id=? AND item_id=?""",
                (plan["run_id"], snapshot["item_id"])).fetchone()
            _require(run["digest"] == plan["digest"] and run["confirmed_digest"] == plan["digest"] and
                     row is not None and row["photo_id"] is None and row["profile_id"] == plan["profile_id"] and
                     row["work_kind"] == "compare" and row["input_fingerprint"] == snapshot["input_fingerprint"] and
                     row["action"] == "compute" and row["status"] == "running" and row["result_id"] is None,
                     "Comparison checkpoint is not the approved running item.", "FEATURE_PLAN_INVALID")
            claim = self.db.execute("""SELECT 1 FROM image_feature_claims
                WHERE run_id=? AND item_id=? AND profile_id=? AND input_fingerprint=? AND scope_key='compare'""",
                (plan["run_id"], snapshot["item_id"], plan["profile_id"], snapshot["input_fingerprint"])).fetchone()
            _require(claim is not None, "Comparison input is no longer claimed.", "FEATURE_IN_PROGRESS")
            progress = {"comparisons": comparisons}
            previous = _load(row["progress_json"])
            _require(isinstance(previous, dict), "Invalid comparison progress.")
            self._validate_item_progress("compare", snapshot, previous)
            self._validate_item_progress("compare", snapshot, progress)
            _require(comparisons >= previous.get("comparisons", 0), "Comparison cursor cannot move backwards.")
            _require(all(_text(photo_id) for photo_id in photo_ids), "Invalid comparison endpoint IDs.")
            endpoints = set(photo_ids)
            for pair in pairs:
                _require(isinstance(pair, dict) and _text(pair.get("photo_id_a")) and _text(pair.get("photo_id_b")),
                         "Invalid similarity pair.")
                endpoints.update((pair["photo_id_a"], pair["photo_id_b"]))
            payloads = {}
            for photo_id in endpoints:
                participant = checkpoint.participants.get(photo_id)
                _require(participant is not None, "Comparison endpoint is outside the approved scope.")
                photo = self.photo(photo_id)
                _require(photo["ingest_state"] == "available" and
                         photo["content_version"] == participant["content_version"],
                         "Comparison photo content changed.", "FEATURE_INPUT_CHANGED")
                if plan["options"]["metric"] == "hamming":
                    from .feature_inputs import verify_manifest

                    source = self._comparison_source(plan, photo_id, participant)
                    verify_manifest(photo_id, plan["profile"], source["input_manifest"], store=self)
                    payloads[photo_id] = source["payload"]
            self._put_similarity_pairs(plan, checkpoint.participants, pairs, verified_payloads=payloads)
            self.db.execute("""UPDATE image_feature_items SET progress_json=?,updated_at=?
                WHERE run_id=? AND item_id=?""", (_json(progress), _timestamp(), plan["run_id"], snapshot["item_id"]))
            token = self.change_token()
        checkpoint.token = token

    def similarity_pairs(self, run_id, *, limit=100, after=0):
        _limit(limit)
        _require(type(after) is int and after >= 0, "Pair cursor must be a nonnegative integer.")
        with self.read_snapshot():
            plan = self.feature_run(run_id)["plan"]
            participants = self._compare_participants(plan)
            rows = self.db.execute("""SELECT * FROM image_similarity_pairs
                WHERE run_id=? AND pair_id>? ORDER BY pair_id LIMIT ?""", (run_id, after, limit + 1)).fetchall()
            total = self.db.execute("SELECT COUNT(*) FROM image_similarity_pairs WHERE run_id=?", (run_id,)).fetchone()[0]
            items = []
            for row in rows[:limit]:
                pair = {key: row[key] for key in (
                    "photo_id_a", "photo_id_b", "result_id_a", "result_id_b",
                    "content_version_a", "content_version_b", "metric", "distance")}
                _require(row["profile_id"] == plan["profile_id"] and row["work_kind"] == "compare" and
                         row["component"] == "perceptual_hash", "Saved comparison pair profile is corrupt.")
                pair = self._validate_pair(plan, participants, pair, current=False)
                items.append({"pair_id": row["pair_id"], "run_id": run_id, "profile_id": row["profile_id"], **pair})
            return {"items": items, "next_cursor": rows[limit - 1]["pair_id"] if len(rows) > limit else None, "total": total}
