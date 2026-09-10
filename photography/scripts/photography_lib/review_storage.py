"""Immutable, queryable reviews and their frozen, resumable batch provenance."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from uuid import uuid4

from .config import PhotographyError
from .fingerprints import fingerprint
from .review_schema import DIMENSIONS, TEXT_LIMIT, dimension_scores, validate_payload, validate_profile
from .thumbnails import stored_preview
from . import review_storage_v1 as legacy_storage


REVIEW_TABLES = ("ai_review_runs", "ai_review_batches", "ai_review_results")
RUN_STATUSES = ("planned", "running", "completed", "partial", "failed", "interrupted", "stale")
BATCH_STATUSES = ("pending", "running", "completed", "failed", "interrupted", "stale")
_INPUT_FIELDS = ("input_scope", "content_version", "thumbnail_profile", "input_image_hash",
                 "width", "height", "size_bytes")
_PROFILE_COLUMNS = ("provider", "model", "language", "rubric_version", "sdk_version", "runtime_version")
_METADATA_NUMBERS = ("elapsed_seconds", "input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens",
                     "cache_write_tokens", "credits", "request_attempts")
_METADATA_TEXT = ("model", "sdk_version", "runtime_version", "provider", "usage_source", "credits_unit")
_METADATA_TEXT_LIMIT = 256
_MAX_NUMBER = 1.7976931348623157e308


def _check(expression):
    return f"CHECK(COALESCE(({expression}),0))"


def _projection(document, key, column=None, kind="text"):
    column = column or key
    types = "'integer','real'" if kind == "number" else repr(kind)
    return _check(f"json_type({document},'$.{key}') IN ({types}) "
                  f"AND json_extract({document},'$.{key}')={column}")


def _exact_object(document, path, keys):
    removals = ",".join(repr("$." + key) for key in keys)
    return _check(f"json_type({document},'{path}')='object' AND "
                  f"json_remove(json_extract({document},'{path}'),{removals})='{{}}'")


def _text_check(expression):
    whitespace = ",".join(str(value) for value in
                          (*range(9, 14), *range(28, 33), 133, 160, 5760, *range(8192, 8203),
                           8232, 8233, 8239, 8287, 12288))
    return f"length(trim({expression},char({whitespace})))>0 AND length({expression})<={TEXT_LIMIT}"


_METADATA_CHECKS = ",\n".join([
    _exact_object("metadata_json", "$", (*_METADATA_NUMBERS, *_METADATA_TEXT)),
    _check("json_type(metadata_json,'$.request_attempts') IS NULL OR "
           "json_type(metadata_json,'$.request_attempts')='integer'"),
    *[_check(f"json_type(metadata_json,'$.{key}') IS NULL OR "
             f"(json_type(metadata_json,'$.{key}') IN ('integer','real') AND "
             f"json_extract(metadata_json,'$.{key}') BETWEEN 0 AND {_MAX_NUMBER})")
      for key in _METADATA_NUMBERS],
    *[_check(f"json_type(metadata_json,'$.{key}') IS NULL OR "
             f"(json_type(metadata_json,'$.{key}')='text' AND "
             f"length(json_extract(metadata_json,'$.{key}'))<={_METADATA_TEXT_LIMIT} AND "
             + _text_check(f"json_extract(metadata_json,'$.{key}')") + ")")
      for key in _METADATA_TEXT],
])
_SCORE_COLUMNS = ",\n".join(
    f"{key}_score REAL CHECK(typeof({key}_score)='real' AND {key}_score BETWEEN 0 AND 10 OR {key}_score IS NULL)"
    for key in DIMENSIONS)
_SQL_MEAN = "(" + "+".join(key + "_score" for key in DIMENSIONS) + ")/6.0"


def _v2_payload_checks():
    document = "payload_json"
    fields = ("schema_version", "overall_score", "review_status", "description", "dimensions",
              "strengths", "improvements", "limitations")
    checks = [
        _exact_object(document, "$", fields),
        _exact_object(document, "$.dimensions", DIMENSIONS),
        _projection(document, "schema_version"), _projection(document, "description"),
        _check("json_type(payload_json,'$.overall_score') IN ('integer','real','null') "
               "AND json_extract(payload_json,'$.overall_score') IS overall_score"),
        _check("json_type(payload_json,'$.review_status')='text' AND "
               "json_extract(payload_json,'$.review_status')=CASE "
               "WHEN " + " AND ".join(key + "_score IS NOT NULL" for key in DIMENSIONS) + " THEN 'reviewed' "
               "WHEN " + " AND ".join(key + "_score IS NULL" for key in DIMENSIONS) + " THEN 'unreviewable' "
               "ELSE 'partial' END"),
    ]
    for key in DIMENSIONS:
        path = "$.dimensions." + key
        checks.extend([
            _exact_object(document, path, ("score", "reason")),
            _check(f"json_type({document},'{path}.score') IN ('integer','real','null') "
                   f"AND json_extract({document},'{path}.score') IS {key}_score "
                   f"AND ({key}_score IS NULL OR {key}_score*2=CAST({key}_score*2 AS INTEGER))"),
            _check(f"json_type({document},'{path}.reason')='text' AND "
                   + _text_check(f"json_extract({document},'{path}.reason')")),
        ])
    for key in ("strengths", "improvements", "limitations"):
        bound = ">=1" if key == "limitations" else "BETWEEN 0 AND 3"
        checks.append(_check(f"json_type({document},'$.{key}')='array' AND "
                             f"json_array_length({document},'$.{key}') {bound}"))
    return ",\n".join(checks)


def _for_version(checks, version):
    return checks.replace("CHECK(COALESCE((", f"CHECK(COALESCE((schema_version<>'{version}' OR (") \
                 .replace("),0))", ")),0))")


_PAYLOAD_CHECKS = _for_version(legacy_storage._PAYLOAD_CHECKS, "photo-review-v1") + ",\n" + \
                  _for_version(_v2_payload_checks(), "photo-review-v2")
_PROVENANCE_CHECKS = ",\n".join([
    _exact_object("input_manifest_json", "$", _INPUT_FIELDS),
    *[_projection("input_manifest_json", key, kind="integer" if key in ("width", "height", "size_bytes") else "text")
      for key in _INPUT_FIELDS],
    *[_projection("profile_json", key) for key in _PROFILE_COLUMNS],
    _projection("profile_json", "input_scope"),
    _projection("profile_json", "output_schema_version", "schema_version"),
    *[_check(f"json_type(metadata_json,'$.{key}') IS NULL OR "
             f"(json_type(metadata_json,'$.{key}')='text' AND json_extract(metadata_json,'$.{key}')={key})")
      for key in _PROFILE_COLUMNS],
])

REVIEW_SCHEMA = (
    f"""CREATE TABLE ai_review_runs (
        run_id TEXT PRIMARY KEY NOT NULL, profile_id TEXT NOT NULL,
        digest TEXT NOT NULL CHECK(length(digest)=64 AND digest NOT GLOB '*[^0-9a-f]*'),
        plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
        status TEXT NOT NULL CHECK(status IN {RUN_STATUSES}),
        revision INTEGER NOT NULL DEFAULT 0 CHECK(typeof(revision)='integer' AND revision>=0),
        request_attempts INTEGER NOT NULL DEFAULT 0 CHECK(typeof(request_attempts)='integer' AND request_attempts>=0),
        elapsed_seconds REAL NOT NULL DEFAULT 0
            CHECK(typeof(elapsed_seconds)='real' AND elapsed_seconds BETWEEN 0 AND 1.7976931348623157e308),
        confirmed_digest TEXT CHECK(confirmed_digest IS NULL OR confirmed_digest=digest),
        approval_history_json TEXT NOT NULL DEFAULT '[]'
            CHECK(json_valid(approval_history_json) AND json_type(approval_history_json)='array'),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(run_id,profile_id),
        {_check("json_type(plan_json)='object' AND json_extract(plan_json,'$.version')='ai-review-plan-v1'")},
        {_projection("plan_json", "run_id")}, {_projection("plan_json", "profile_id")},
        {_projection("plan_json", "digest")}, {_projection("plan_json", "created_at")})""",
    f"""CREATE TABLE ai_review_batches (
        batch_id TEXT PRIMARY KEY NOT NULL, run_id TEXT NOT NULL REFERENCES ai_review_runs(run_id),
        ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal>=0),
        items_json TEXT NOT NULL CHECK(json_valid(items_json)),
        status TEXT NOT NULL CHECK(status IN {BATCH_STATUSES}),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(typeof(attempts)='integer' AND attempts>=0),
        error_json TEXT CHECK(error_json IS NULL OR (json_valid(error_json) AND json_type(error_json)='object')),
        metadata_json TEXT NOT NULL DEFAULT '{{}}'
            CHECK(json_valid(metadata_json) AND json_type(metadata_json)='object'),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(run_id,ordinal), UNIQUE(batch_id,run_id),
        {_check("json_type(items_json)='array' AND json_array_length(items_json)>0")},
        {_METADATA_CHECKS})""",
    f"""CREATE TABLE ai_review_results (
        result_id TEXT PRIMARY KEY NOT NULL, photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        run_id TEXT NOT NULL, batch_id TEXT NOT NULL, profile_id TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint)=64 AND input_fingerprint NOT GLOB '*[^0-9a-f]*'),
        profile_json TEXT NOT NULL CHECK(json_valid(profile_json) AND json_type(profile_json)='object'),
        input_manifest_json TEXT NOT NULL CHECK(json_valid(input_manifest_json)),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json) AND json_type(metadata_json)='object'),
        schema_version TEXT NOT NULL CHECK(schema_version IN ('photo-review-v1','photo-review-v2')),
        rubric_version TEXT NOT NULL CHECK(rubric_version=schema_version),
        provider TEXT NOT NULL CHECK(provider='github-copilot'), model TEXT NOT NULL,
        language TEXT NOT NULL CHECK(language IN ('zh-CN','en')),
        sdk_version TEXT NOT NULL, runtime_version TEXT NOT NULL,
        input_scope TEXT NOT NULL CHECK(input_scope='stored_thumbnail'),
        content_version TEXT NOT NULL, thumbnail_profile TEXT NOT NULL, input_image_hash TEXT NOT NULL,
        width INTEGER NOT NULL CHECK(typeof(width)='integer' AND width BETWEEN 1 AND 4096),
        height INTEGER NOT NULL CHECK(typeof(height)='integer' AND height BETWEEN 1 AND 4096),
        size_bytes INTEGER NOT NULL CHECK(typeof(size_bytes)='integer' AND size_bytes>0),
        description TEXT NOT NULL CHECK({_text_check("description")}),
        {_SCORE_COLUMNS},
        overall_score REAL CHECK(overall_score IS NULL OR (typeof(overall_score)='real' AND overall_score BETWEEN 0 AND 10)),
        created_at TEXT NOT NULL CHECK(length(created_at)=32 AND substr(created_at,27)='+00:00'
            AND datetime(created_at) IS NOT NULL),
        UNIQUE(photo_id,batch_id),
        FOREIGN KEY(run_id,profile_id) REFERENCES ai_review_runs(run_id,profile_id),
        FOREIGN KEY(batch_id,run_id) REFERENCES ai_review_batches(batch_id,run_id),
        {_PAYLOAD_CHECKS}, {_PROVENANCE_CHECKS}, {_METADATA_CHECKS},
        {_check("(overall_score IS NULL)=(" + " OR ".join(key + "_score IS NULL" for key in DIMENSIONS) + ")")},
        CHECK(overall_score=round(overall_score,2)),
        CHECK(overall_score BETWEEN round({_SQL_MEAN}-0.00000000000001,2)
            AND round({_SQL_MEAN}+0.00000000000001,2)),
        CHECK(length(result_id)=68 AND substr(result_id,-32) NOT GLOB '*[^0-9a-f]*'),
        CHECK(result_id='review_result_'||replace(replace(replace(substr(created_at,1,26),'-',''),':',''),'T','')
            ||'_'||substr(result_id,-32)))""",
    "CREATE INDEX ai_review_cache ON ai_review_results(photo_id,profile_id,input_fingerprint,result_id DESC)",
    "CREATE INDEX ai_review_history ON ai_review_results(photo_id,result_id DESC)",
    "CREATE INDEX ai_review_created ON ai_review_results(created_at DESC,result_id DESC)",
    "CREATE INDEX ai_review_result_batch ON ai_review_results(batch_id,photo_id)",
    *[f"CREATE INDEX ai_review_{key}_score ON ai_review_results({key}_score,result_id)"
      for key in ("overall", *DIMENSIONS)],
    """CREATE TRIGGER ai_review_plan_immutable
        BEFORE UPDATE OF run_id,profile_id,digest,plan_json,created_at ON ai_review_runs
        BEGIN SELECT RAISE(ABORT,'Review plans are immutable'); END""",
    """CREATE TRIGGER ai_review_batch_immutable
        BEFORE UPDATE OF batch_id,run_id,ordinal,items_json,created_at ON ai_review_batches
        BEGIN SELECT RAISE(ABORT,'Review batches are immutable'); END""",
    """CREATE TRIGGER ai_review_result_immutable BEFORE UPDATE ON ai_review_results
        BEGIN SELECT RAISE(ABORT,'Review results are immutable'); END""",
    """CREATE TRIGGER ai_review_result_preserved BEFORE DELETE ON ai_review_results
        BEGIN SELECT RAISE(ABORT,'Review history is immutable'); END""",
    """CREATE TRIGGER ai_review_plan_preserved BEFORE DELETE ON ai_review_runs
        BEGIN SELECT RAISE(ABORT,'Review plans preserve result provenance'); END""",
    """CREATE TRIGGER ai_review_batch_deleted BEFORE DELETE ON ai_review_batches
        BEGIN SELECT RAISE(ABORT,'Review batches preserve result provenance'); END""",
    """CREATE TRIGGER ai_review_batch_membership BEFORE INSERT ON ai_review_batches
        WHEN NOT EXISTS(SELECT 1 FROM ai_review_runs r,json_each(r.plan_json,'$.batches') b
            WHERE r.run_id=new.run_id AND json_extract(b.value,'$.batch_id')=new.batch_id
            AND json_extract(b.value,'$.ordinal')=new.ordinal
            AND json_extract(b.value,'$.items')=new.items_json)
        BEGIN SELECT RAISE(ABORT,'Review batch does not match the frozen plan'); END""",
    """CREATE TRIGGER ai_review_result_membership BEFORE INSERT ON ai_review_results
        WHEN NOT EXISTS(SELECT 1 FROM ai_review_runs r
            JOIN ai_review_batches b ON b.run_id=r.run_id JOIN json_each(b.items_json) i
            WHERE r.run_id=new.run_id AND b.batch_id=new.batch_id AND b.status='running'
            AND r.status='running' AND r.confirmed_digest=r.digest
            AND r.profile_id=new.profile_id AND json_extract(r.plan_json,'$.profile')=new.profile_json
            AND json_extract(i.value,'$.photo_id')=new.photo_id AND json_extract(i.value,'$.action')='review'
            AND json_extract(i.value,'$.input_fingerprint')=new.input_fingerprint
            AND json_extract(i.value,'$.input_manifest')=new.input_manifest_json)
        BEGIN SELECT RAISE(ABORT,'Review result does not match its planned batch input'); END""",
    """CREATE TRIGGER ai_review_result_current BEFORE INSERT ON ai_review_results
        WHEN NOT EXISTS(SELECT 1 FROM photos p JOIN thumbnails t ON t.photo_id=p.photo_id
            WHERE p.photo_id=new.photo_id AND p.content_version=new.content_version
            AND p.thumbnail_profile=new.thumbnail_profile AND t.content_version=new.content_version
            AND t.profile=new.thumbnail_profile AND t.image_hash=new.input_image_hash
            AND t.width=new.width AND t.height=new.height AND t.size_bytes=new.size_bytes
            AND length(t.data)=new.size_bytes AND t.mime_type='image/jpeg')
        BEGIN SELECT RAISE(ABORT,'Review result input is no longer current'); END""",
    f"""CREATE TRIGGER ai_review_result_lists BEFORE INSERT ON ai_review_results
        WHEN EXISTS(SELECT 1 FROM (
            SELECT type,value FROM json_each(new.payload_json,'$.strengths')
            UNION ALL SELECT type,value FROM json_each(new.payload_json,'$.improvements')
                WHERE new.schema_version='photo-review-v1'
            UNION ALL SELECT type,value FROM json_each(new.payload_json,'$.limitations')) item
            WHERE item.type<>'text' OR NOT ({_text_check("item.value")}))
        BEGIN SELECT RAISE(ABORT,'Review lists must contain bounded nonempty text'); END""",
    f"""CREATE TRIGGER ai_review_result_improvements BEFORE INSERT ON ai_review_results
        WHEN new.schema_version='photo-review-v2' AND EXISTS(
            SELECT 1 FROM json_each(new.payload_json,'$.improvements') item
            WHERE item.type<>'object' OR NOT COALESCE((
                json_remove(item.value,'$.kind','$.action','$.rationale','$.tradeoff')='{{}}'
                AND json_extract(item.value,'$.kind') IN ('edit','reshoot')
                AND json_type(item.value,'$.action')='text' AND {_text_check("json_extract(item.value,'$.action')")}
                AND json_type(item.value,'$.rationale')='text' AND {_text_check("json_extract(item.value,'$.rationale')")}
                AND (json_type(item.value,'$.tradeoff')='null' OR
                    (json_type(item.value,'$.tradeoff')='text' AND {_text_check("json_extract(item.value,'$.tradeoff')")}))
            ),0))
        BEGIN SELECT RAISE(ABORT,'Invalid structured review improvement'); END""",
    """CREATE TRIGGER ai_review_run_initial BEFORE INSERT ON ai_review_runs
        WHEN new.status<>'planned' OR new.revision<>0 OR new.request_attempts<>0 OR new.elapsed_seconds<>0
            OR new.confirmed_digest IS NOT NULL OR new.approval_history_json<>'[]'
        BEGIN SELECT RAISE(ABORT,'Review runs must begin unapproved and planned'); END""",
    """CREATE TRIGGER ai_review_batch_initial BEFORE INSERT ON ai_review_batches
        WHEN new.status<>'pending' OR new.attempts<>0 OR new.error_json IS NOT NULL OR new.metadata_json<>'{}'
        BEGIN SELECT RAISE(ABORT,'Review batches must begin pending'); END""",
    """CREATE TRIGGER ai_review_run_progress BEFORE UPDATE ON ai_review_runs
        WHEN new.revision<old.revision OR new.request_attempts<old.request_attempts OR new.elapsed_seconds<old.elapsed_seconds
            OR json_array_length(new.approval_history_json)<json_array_length(old.approval_history_json)
            OR EXISTS(SELECT 1 FROM json_each(old.approval_history_json) entry
                WHERE entry.value IS NOT json_extract(new.approval_history_json,'$['||entry.key||']'))
        BEGIN SELECT RAISE(ABORT,'Review attempts and approval history cannot move backwards'); END""",
    """CREATE TRIGGER ai_review_batch_progress BEFORE UPDATE ON ai_review_batches
        WHEN new.attempts<old.attempts
        BEGIN SELECT RAISE(ABORT,'Review batch attempts cannot move backwards'); END""",
    """CREATE TRIGGER ai_review_batch_completed BEFORE UPDATE OF status ON ai_review_batches
        WHEN new.status='completed' AND (SELECT count(*) FROM ai_review_results WHERE batch_id=new.batch_id)
            <>json_array_length(new.items_json)
        BEGIN SELECT RAISE(ABORT,'A completed review batch requires every result'); END""",
    """CREATE TRIGGER ai_review_batch_preserved BEFORE UPDATE ON ai_review_batches
        WHEN old.status='completed' AND (new.status<>old.status OR new.attempts<>old.attempts
            OR new.error_json IS NOT old.error_json OR new.metadata_json<>old.metadata_json)
        BEGIN SELECT RAISE(ABORT,'Completed review batches cannot be retried'); END""",
    """CREATE TRIGGER ai_review_run_completed BEFORE UPDATE OF status ON ai_review_runs
        WHEN new.status='completed' AND EXISTS(
            SELECT 1 FROM ai_review_batches WHERE run_id=new.run_id AND status<>'completed')
        BEGIN SELECT RAISE(ABORT,'A completed review run requires every batch'); END""",
)


def _require(condition, message, code="REVIEW_STORAGE_INVALID"):
    if not condition:
        raise PhotographyError(code, message)


def _json(value):
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        text.encode("utf-8")
        return text
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise PhotographyError("REVIEW_STORAGE_INVALID", "Expected finite, structured review data.") from exc


def _load(text):
    def pairs(entries):
        result = {}
        for key, value in entries:
            _require(key not in result, "Saved review JSON contains duplicate keys.")
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=pairs)
        _json(value).encode("utf-8")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise PhotographyError("REVIEW_STORAGE_INVALID", "Saved review JSON is invalid.") from exc


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _metadata(value, profile=None):
    _require(type(value) is dict, "Review metadata must be a structured object.")
    result = {}
    for key, item in value.items():
        if key not in (*_METADATA_NUMBERS, *_METADATA_TEXT) or item is None:
            continue
        if key in _METADATA_NUMBERS:
            _require(type(item) in (int, float) and 0 <= item <= _MAX_NUMBER and math.isfinite(item),
                     "Review usage measurements must be finite nonnegative numbers, not booleans.")
            _require(key != "request_attempts" or type(item) is int,
                     "Review request-attempt metadata must be an integer.")
        else:
            _require(_text(item) and len(item) <= _METADATA_TEXT_LIMIT,
                     "Review usage labels must be bounded nonempty strings.")
            if profile is not None and key in _PROFILE_COLUMNS:
                _require(item == profile[key], "Review usage identity disagrees with the frozen profile.")
        result[key] = item
    _json(result)
    return result


def _manifest(value):
    _require(type(value) is dict and set(value) == set(_INPUT_FIELDS), "Invalid review input manifest.")
    _require(value["input_scope"] == "stored_thumbnail" and _hash(value["content_version"])
             and _hash(value["input_image_hash"]) and _text(value["thumbnail_profile"])
             and all(_integer(value[key], 1) for key in ("width", "height", "size_bytes"))
             and max(value["width"], value["height"]) <= 4096, "Invalid review input identity.")
    return value


@lru_cache(maxsize=2)
def review_schema_registry(version=12):
    with closing(sqlite3.connect(":memory:")) as reference:
        for statement in (legacy_storage.REVIEW_SCHEMA if version == 11 else REVIEW_SCHEMA):
            reference.execute(statement)
        objects = {(row[0], row[1]): row[2] for row in reference.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*'")}
        columns = {table: {row[1] for row in reference.execute(f'PRAGMA table_info("{table}")')}
                   for table in REVIEW_TABLES}
    return objects, columns


@contextmanager
def _write(store):
    try:
        with store.transaction():
            before = store.db.total_changes
            yield
            store._record_photo_changes(before)
    except sqlite3.IntegrityError as exc:
        raise PhotographyError("REVIEW_CONFLICT", "Review identity or integrity constraint failed.") from exc


class ReviewStorage:
    def _validate_registered_schema(self, tables, registry):
        expected, columns = registry
        actual = {(row["type"], row["name"]): row["sql"] for row in self.db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL")
            if row["tbl_name"] in tables or (row["type"], row["name"]) in expected}
        normalize = lambda sql: re.sub(r"\s+", " ", sql.strip())
        _require(set(actual) == set(expected) and all(
            normalize(actual[key]) == normalize(sql) for key, sql in expected.items()),
            "Album schema contains missing, changed or unregistered objects.", "SCHEMA_INVALID")
        for table, names in columns.items():
            actual_names = {row["name"] for row in self.db.execute(f'PRAGMA table_info("{table}")')}
            _require(actual_names == names, f"Album table columns do not match: {table}.", "SCHEMA_INVALID")
        return expected

    def validate_review_schema(self):
        self._validate_registered_schema(REVIEW_TABLES, review_schema_registry(
            self.db.execute("PRAGMA user_version").fetchone()[0]))

    def _review_input_current(self, photo_id, manifest):
        photo = self.photo(photo_id)
        _require(photo["ingest_state"] == "available", "The selected photo needs reingestion.", "REVIEW_INPUT_STALE")
        data = stored_preview(photo, self)
        thumbnail = self.thumbnail(photo_id, include_data=False)
        current = {"input_scope": "stored_thumbnail", "content_version": photo["content_version"],
                   "thumbnail_profile": thumbnail["profile"], "input_image_hash": thumbnail["image_hash"],
                   "width": thumbnail["width"], "height": thumbnail["height"], "size_bytes": len(data)}
        _require(manifest == current and thumbnail["size_bytes"] == len(data),
                 "The stored review input has changed.", "REVIEW_INPUT_STALE")

    def _review_plan(self, plan, *, current=False):
        expected = {"version", "run_id", "album_id", "created_at", "profile", "profile_id",
                    "batch_size", "force", "items", "batches", "counts", "max_image_bytes", "disclosure", "digest"}
        selection_fields = {"selection_count", "duplicates_removed"}
        _require(type(plan) is dict and set(plan) - {"max_concurrency"} in (expected, expected | selection_fields),
                 "Invalid review plan fields.")
        _require(plan["version"] == "ai-review-plan-v1" and _text(plan["run_id"])
                 and plan["album_id"] == self.album()["id"] and _text(plan["created_at"])
                 and _integer(plan["batch_size"], 1) and _integer(plan["max_image_bytes"])
                 and _integer(plan.get("max_concurrency", 1), 1)
                 and type(plan["force"]) is bool and isinstance(plan["disclosure"], (str, dict, list))
                 and bool(plan["disclosure"]), "Invalid review plan identity or limits.")
        try:
            _require(datetime.fromisoformat(plan["created_at"]).tzinfo is not None, "Review timestamps require a timezone.")
        except (ValueError, TypeError) as exc:
            raise PhotographyError("REVIEW_STORAGE_INVALID", "Invalid review creation timestamp.") from exc
        validate_profile(plan["profile"])
        _require(fingerprint(plan["profile"]) == plan["profile_id"]
                 and fingerprint({key: value for key, value in plan.items() if key != "digest"}) == plan["digest"],
                 "Review plan failed its digest or profile identity check.")
        _require(type(plan["items"]) is list and bool(plan["items"]) and type(plan["batches"]) is list,
                 "A review plan requires selected photos and ordered batches.")
        if selection_fields <= set(plan):
            _require(_integer(plan["selection_count"], 1) and _integer(plan["duplicates_removed"])
                     and plan["selection_count"] == len(plan["items"]) + plan["duplicates_removed"],
                     "Review selection and duplicate counts are inconsistent.")
        seen, pending = set(), []
        for item in plan["items"]:
            _require(type(item) is dict and set(item) ==
                     {"photo_id", "input_manifest", "input_fingerprint", "action", "result_id"},
                     "Invalid review photo snapshot.")
            _require(_text(item["photo_id"]) and item["photo_id"] not in seen, "Review photo IDs must be distinct.")
            seen.add(item["photo_id"])
            _manifest(item["input_manifest"])
            _require(item["input_fingerprint"] == fingerprint(item["input_manifest"])
                     and (item["action"] == "reuse" or item["input_manifest"]["size_bytes"] <= plan["max_image_bytes"]),
                     "Review snapshot identity or image limit does not match.")
            _require(item["action"] in ("review", "reuse") and
                     ((item["action"] == "review" and item["result_id"] is None) or
                      (item["action"] == "reuse" and _text(item["result_id"]) and not plan["force"])),
                     "Invalid review snapshot action.")
            if current:
                self._review_input_current(item["photo_id"], item["input_manifest"])
                if item["action"] == "reuse":
                    result = self.review_result(item["result_id"])
                    _require((result["photo_id"], result["profile_id"], result["input_fingerprint"]) ==
                             (item["photo_id"], plan["profile_id"], item["input_fingerprint"]),
                             "A cached review does not match the selected input.")
            if item["action"] == "review":
                pending.append(item)
        batch_ids, flattened = set(), []
        for ordinal, batch in enumerate(plan["batches"]):
            _require(type(batch) is dict and set(batch) == {"batch_id", "ordinal", "items"}
                     and _text(batch["batch_id"]) and batch["batch_id"] not in batch_ids
                     and type(batch["ordinal"]) is int and batch["ordinal"] == ordinal
                     and type(batch["items"]) is list and 1 <= len(batch["items"]) <= plan["batch_size"],
                     "Invalid review batch identity or membership.")
            batch_ids.add(batch["batch_id"])
            flattened.extend(batch["items"])
            _require(batch["items"] == pending[ordinal * plan["batch_size"]:(ordinal + 1) * plan["batch_size"]],
                     "Review batches must preserve the frozen pending selection order.")
        _require(type(plan["counts"]) is dict and all(_integer(value) for value in plan["counts"].values())
                 and flattened == pending and plan["counts"] ==
                 {"total": len(seen), "cached": len(seen) - len(pending),
                  "pending": len(pending), "batches": len(plan["batches"])},
                 "Review counts or batch coverage do not match the selection.")
        return plan

    def create_review_run(self, plan):
        with _write(self):
            plan = self._review_plan(_load(_json(plan)), current=True)
            if (plan["profile"]["output_schema_version"] == "photo-review-v2"
                    and self.db.execute("PRAGMA user_version").fetchone()[0] == 11):
                raise PhotographyError("REVIEW_UPGRADE_REQUIRED",
                                       "Use review upgrade --output <new-album.sqlite> and select that copy for v2 reviews.")
            stamp = _timestamp()
            self.db.execute("""INSERT INTO ai_review_runs
                (run_id,profile_id,digest,plan_json,status,created_at,updated_at)
                VALUES (?,?,?,?,'planned',?,?)""",
                (plan["run_id"], plan["profile_id"], plan["digest"], _json(plan), plan["created_at"], stamp))
            for batch in plan["batches"]:
                self.db.execute("""INSERT INTO ai_review_batches
                    (batch_id,run_id,ordinal,items_json,status,created_at,updated_at)
                    VALUES (?,?,?,?,'pending',?,?)""",
                    (batch["batch_id"], plan["run_id"], batch["ordinal"], _json(batch["items"]), stamp, stamp))

    def review_run(self, run_id):
        with self.read_snapshot():
            row = self.db.execute("SELECT * FROM ai_review_runs WHERE run_id=?", (run_id,)).fetchone()
            _require(row is not None, "Review run does not exist.", "REVIEW_RUN_NOT_FOUND")
            result = dict(row)
            result["plan"] = self._review_plan(_load(result.pop("plan_json")))
            result["approval_history"] = _load(result.pop("approval_history_json"))
            _require(all(result[key] == result["plan"][key] for key in
                         ("run_id", "profile_id", "digest", "created_at")), "Saved review run identity is inconsistent.")
            self._review_run_progress(result)
            return result

    @staticmethod
    def _review_run_progress(result):
        _require(result["status"] in RUN_STATUSES and _integer(result["revision"])
                 and _integer(result["request_attempts"])
                 and type(result["elapsed_seconds"]) in (int, float)
                 and 0 <= result["elapsed_seconds"] <= _MAX_NUMBER and math.isfinite(result["elapsed_seconds"])
                 and result["confirmed_digest"] in (None, result["digest"])
                 and type(result["approval_history"]) is list,
                 "Invalid review run progress.")
        _json(result["approval_history"])

    def review_batches(self, run_id):
        with self.read_snapshot():
            plan = self.review_run(run_id)["plan"]
            rows = self.db.execute("SELECT * FROM ai_review_batches WHERE run_id=? ORDER BY ordinal",
                                   (run_id,)).fetchall()
            _require(len(rows) == len(plan["batches"]), "Saved review batches are incomplete.")
            result = []
            for row, frozen in zip(rows, plan["batches"]):
                batch = dict(row)
                batch["items"] = _load(batch.pop("items_json"))
                batch["error"] = _load(batch.pop("error_json")) if row["error_json"] is not None else None
                batch["metadata"] = _load(batch.pop("metadata_json"))
                _require(all(batch[key] == frozen[key] for key in ("batch_id", "ordinal", "items"))
                         and batch["status"] in BATCH_STATUSES and _integer(batch["attempts"])
                         and (batch["error"] is None or type(batch["error"]) is dict)
                         and _metadata(batch["metadata"]) == batch["metadata"],
                         "Saved review batch identity or progress is inconsistent.")
                result.append(batch)
            return result

    def update_review_run(self, run_id, **changes):
        _require(set(changes) <= {"status", "revision", "request_attempts", "confirmed_digest", "approval_history", "elapsed_seconds"},
                 "Only review run progress may be changed.")
        with _write(self):
            current = self.review_run(run_id)
            updated = {**current, **changes}
            self._review_run_progress(updated)
            _require(updated["revision"] >= current["revision"]
                     and updated["request_attempts"] >= current["request_attempts"]
                     and updated["elapsed_seconds"] >= current["elapsed_seconds"]
                     and updated["approval_history"][:len(current["approval_history"])] == current["approval_history"],
                     "Review attempt counters and approval history cannot move backwards.")
            values = {("approval_history_json" if key == "approval_history" else key):
                      (_json(value) if key == "approval_history" else value) for key, value in changes.items()}
            values["updated_at"] = _timestamp()
            self.db.execute("UPDATE ai_review_runs SET " + ",".join(key + "=?" for key in values) + " WHERE run_id=?",
                            (*values.values(), run_id))

    def update_review_batch(self, batch_id, **changes):
        _require(set(changes) <= {"status", "attempts", "error", "metadata"}, "Only review batch progress may be changed.")
        with _write(self):
            row = self.db.execute("SELECT run_id FROM ai_review_batches WHERE batch_id=?", (batch_id,)).fetchone()
            _require(row is not None, "Review batch does not exist.", "REVIEW_BATCH_NOT_FOUND")
            current = next(batch for batch in self.review_batches(row["run_id"]) if batch["batch_id"] == batch_id)
            updated = {**current, **changes}
            _require(updated["status"] in BATCH_STATUSES and _integer(updated["attempts"])
                     and updated["attempts"] >= current["attempts"]
                     and (updated["error"] is None or type(updated["error"]) is dict)
                     and type(updated["metadata"]) is dict, "Invalid review batch progress.")
            if "metadata" in changes:
                changes["metadata"] = _metadata(changes["metadata"])
            values = {(key + "_json" if key in ("error", "metadata") else key):
                      (_json(value) if key in ("error", "metadata") and value is not None else value)
                      for key, value in changes.items()}
            values["updated_at"] = _timestamp()
            self.db.execute("UPDATE ai_review_batches SET " + ",".join(key + "=?" for key in values) + " WHERE batch_id=?",
                            (*values.values(), batch_id))

    def put_review_result(self, photo_id, run_id, batch_id, profile, input_manifest, payload, metadata):
        with _write(self):
            profile = validate_profile(profile)
            # Exact decimal half-up validation belongs here; SQLite's binary mean
            # constraint allows its rounding uncertainty at a half-cent boundary.
            payload = validate_payload(payload)
            _require(payload["schema_version"] == profile["output_schema_version"],
                     "Review payload version does not match its frozen profile.")
            _manifest(input_manifest)
            metadata = _metadata(metadata, profile)
            run = self.review_run(run_id)
            batches = self.review_batches(run_id)
            batch = next((item for item in batches if item["batch_id"] == batch_id), None)
            _require(run["status"] == "running" and run["confirmed_digest"] == run["digest"]
                     and batch is not None and batch["status"] == "running",
                     "Review results require the confirmed original plan and a running run and batch.")
            item = next((item for item in batch["items"] if item["photo_id"] == photo_id), None)
            _require(item is not None and item["action"] == "review"
                     and item["input_manifest"] == input_manifest and run["plan"]["profile"] == profile,
                     "Review result provenance does not match its frozen batch.")
            self._review_input_current(photo_id, input_manifest)
            stamp = _timestamp()
            latest = self.db.execute("SELECT created_at FROM ai_review_results ORDER BY result_id DESC LIMIT 1").fetchone()
            if latest is not None and stamp <= latest["created_at"]:
                stamp = (datetime.fromisoformat(latest["created_at"]) + timedelta(microseconds=1)).isoformat(timespec="microseconds")
            result_id = "review_result_" + stamp[:26].replace("-", "").replace(":", "").replace("T", "") + "_" + uuid4().hex
            record = {
                "result_id": result_id, "photo_id": photo_id, "run_id": run_id, "batch_id": batch_id,
                "profile_id": fingerprint(profile), "input_fingerprint": fingerprint(input_manifest),
                "profile_json": _json(profile), "input_manifest_json": _json(input_manifest),
                "payload_json": _json(payload), "metadata_json": _json(metadata),
                "schema_version": payload["schema_version"], **{key: profile[key] for key in _PROFILE_COLUMNS},
                **input_manifest, "description": payload["description"], "overall_score": payload["overall_score"],
                **{key + "_score": dimension_scores(payload)[key]["score"] for key in DIMENSIONS}, "created_at": stamp,
            }
            self.db.execute("INSERT INTO ai_review_results (" + ",".join(record) + ") VALUES ("
                            + ",".join("?" for _ in record) + ")", tuple(record.values()))
            return result_id

    def review_result(self, result_id):
        with self.read_snapshot():
            row = self.db.execute("SELECT * FROM ai_review_results WHERE result_id=?", (result_id,)).fetchone()
            _require(row is not None, "Review result does not exist.", "REVIEW_RESULT_NOT_FOUND")
            result = dict(row)
            for key in ("payload", "profile", "input_manifest", "metadata"):
                result[key] = _load(result.pop(key + "_json"))
            validate_payload(result["payload"])
            validate_profile(result["profile"])
            _manifest(result["input_manifest"])
            _require(_metadata(result["metadata"], result["profile"]) == result["metadata"]
                     and result["profile_id"] == fingerprint(result["profile"])
                     and result["input_fingerprint"] == fingerprint(result["input_manifest"])
                     and result["schema_version"] == result["profile"]["output_schema_version"]
                     and all(result[key] == result["profile"][key] for key in _PROFILE_COLUMNS)
                     and all(result[key] == result["input_manifest"][key] for key in _INPUT_FIELDS)
                     and all(result[key] == result["payload"][key] for key in
                             ("schema_version", "description", "overall_score"))
                     and all(result["metadata"].get(key, result[key]) == result[key] for key in _PROFILE_COLUMNS)
                     and all(result[key + "_score"] == dimension_scores(result["payload"])[key]["score"] for key in DIMENSIONS),
                     "Saved review payload and searchable projections disagree.")
            run = self.review_run(result["run_id"])
            batch = next((batch for batch in self.review_batches(result["run_id"])
                          if batch["batch_id"] == result["batch_id"]), None)
            _require(run["plan"]["profile"] == result["profile"] and batch is not None and
                     any(item["photo_id"] == result["photo_id"] and item["action"] == "review"
                         and item["input_manifest"] == result["input_manifest"] for item in batch["items"]),
                     "Saved review result does not match its frozen provenance.")
            return result

    def find_review_result(self, photo_id, profile_id, input_fingerprint):
        with self.read_snapshot():
            row = self.db.execute("""SELECT result_id FROM ai_review_results
                WHERE photo_id=? AND profile_id=? AND input_fingerprint=? ORDER BY result_id DESC LIMIT 1""",
                (photo_id, profile_id, input_fingerprint)).fetchone()
            return self.review_result(row["result_id"]) if row is not None else None

    def review_run_results(self, run_id: str) -> list[dict]:
        with self.read_snapshot():
            self.review_batches(run_id)
            rows = self.db.execute("""SELECT r.result_id FROM ai_review_results r
                JOIN ai_review_batches b ON r.batch_id=b.batch_id AND r.run_id=b.run_id
                WHERE r.run_id=? ORDER BY b.ordinal,r.photo_id,r.result_id""", (run_id,)).fetchall()
            return [self.review_result(row["result_id"]) for row in rows]

    def review_history(self, photo_id, *, limit=100, after=""):
        self._limit(limit)
        _require(isinstance(after, str), "Review history cursor must be an opaque result ID.", "INVALID_ARGUMENT")
        with self.read_snapshot():
            self.photo(photo_id)
            rows = self.db.execute("""SELECT result_id FROM ai_review_results WHERE photo_id=?
                AND (?='' OR result_id<?) ORDER BY result_id DESC LIMIT ?""", (photo_id, after, after, limit)).fetchall()
            return [self.review_result(row["result_id"]) for row in rows]
