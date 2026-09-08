"""Frozen schema-11 SQL registry used to validate and copy historical albums."""
from .review_schema_v1 import DIMENSIONS, LIST_LIMIT, TEXT_LIMIT


REVIEW_TABLES = ("ai_review_runs", "ai_review_batches", "ai_review_results")
RUN_STATUSES = ("planned", "running", "completed", "partial", "failed", "interrupted", "stale")
BATCH_STATUSES = ("pending", "running", "completed", "failed", "interrupted", "stale")
_INPUT_FIELDS = ("input_scope", "content_version", "thumbnail_profile", "input_image_hash",
                 "width", "height", "size_bytes")
_PROFILE_COLUMNS = ("provider", "model", "language", "rubric_version", "sdk_version", "runtime_version")
_PAYLOAD_FIELDS = ("schema_version", "overall_score", "description", "strengths",
                   "improvements", "limitations", "scores")
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
    f"{key}_score REAL NOT NULL CHECK(typeof({key}_score)='real' AND {key}_score BETWEEN 0 AND 10)"
    for key in DIMENSIONS)
_SQL_MEAN = "(" + "+".join(key + "_score" for key in DIMENSIONS) + ")/6.0"
_PAYLOAD_CHECKS = ",\n".join([
    _exact_object("payload_json", "$", _PAYLOAD_FIELDS),
    _exact_object("payload_json", "$.scores", DIMENSIONS),
    _projection("payload_json", "schema_version"),
    _projection("payload_json", "description"),
    _projection("payload_json", "overall_score", kind="number"),
    *[_projection("payload_json", f"scores.{key}.score", f"{key}_score", "number") for key in DIMENSIONS],
    *[_exact_object("payload_json", f"$.scores.{key}", ("score", "reason")) for key in DIMENSIONS],
    *[_check(f"json_type(payload_json,'$.scores.{key}.reason')='text' AND "
             + _text_check(f"json_extract(payload_json,'$.scores.{key}.reason')")) for key in DIMENSIONS],
    *[_check(f"json_type(payload_json,'$.{key}')='array' AND "
             f"json_array_length(payload_json,'$.{key}') BETWEEN 1 AND {LIST_LIMIT}")
      for key in ("strengths", "improvements", "limitations")],
])
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
        schema_version TEXT NOT NULL CHECK(schema_version='photo-review-v1'),
        rubric_version TEXT NOT NULL CHECK(rubric_version='photo-review-v1'),
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
        overall_score REAL NOT NULL CHECK(typeof(overall_score)='real' AND overall_score BETWEEN 0 AND 10),
        created_at TEXT NOT NULL CHECK(length(created_at)=32 AND substr(created_at,27)='+00:00'
            AND datetime(created_at) IS NOT NULL),
        UNIQUE(photo_id,batch_id),
        FOREIGN KEY(run_id,profile_id) REFERENCES ai_review_runs(run_id,profile_id),
        FOREIGN KEY(batch_id,run_id) REFERENCES ai_review_batches(batch_id,run_id),
        {_PAYLOAD_CHECKS}, {_PROVENANCE_CHECKS}, {_METADATA_CHECKS},
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
            UNION ALL SELECT type,value FROM json_each(new.payload_json,'$.limitations')) item
            WHERE item.type<>'text' OR NOT ({_text_check("item.value")}))
        BEGIN SELECT RAISE(ABORT,'Review lists must contain bounded nonempty text'); END""",
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
