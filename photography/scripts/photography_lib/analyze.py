"""Serial, resumable photo analysis with per-photo atomic persistence."""
from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from .analysis_schema import fingerprint, validate_analysis
from .config import Config, PhotographyError
from .sqlite_storage import SQLiteStorage, now
from .storage import AnalysisStorage
from .vision import OpenAIResponsesProvider, ProviderError, VisionProvider
from .thumbnails import MAX_PREVIEW_BYTES, stored_preview

def unique_ids(photo_ids) -> list[str]:
    if not isinstance(photo_ids, (list, tuple)) or not photo_ids or any(
        not isinstance(item, str) or not item.strip() for item in photo_ids
    ):
        raise PhotographyError("INVALID_ARGUMENT", "Provide a non-empty list of photo IDs.")
    return list(dict.fromkeys(photo_ids))


def read_preview(photo: dict, config: Config, store) -> bytes:
    if photo["state"] != "available":
        raise PhotographyError("PHOTO_UNAVAILABLE", "Photo is missing or has an ingestion error.")
    try:
        source_stat = Path(photo["original_path"]).stat()
    except OSError:
        raise PhotographyError("PHOTO_UNAVAILABLE", "Original photo is not accessible; rescan the library.") from None
    if (source_stat.st_size, source_stat.st_mtime_ns) != (photo["size_bytes"], photo["mtime_ns"]):
        raise PhotographyError("PHOTO_CHANGED", "Original photo changed since ingestion; rescan it before analysis.")
    return stored_preview(photo, store)


def prepare(photo_id, store, config, config_id):
    photo = store.photo(photo_id)
    preview = read_preview(photo, config, store)
    image_hash = hashlib.sha256(preview).hexdigest()
    key = fingerprint({"photo_id": photo_id, "content_version": photo["content_version"],
                       "input_image_hash": image_hash, "thumbnail_profile": photo["thumbnail_profile"],
                       "analysis_config_id": config_id})
    return photo, preview, image_hash, key


def assert_current(photo, image_hash, store, config):
    current = store.photo(photo["photo_id"])
    if (current["content_version"], current["thumbnail_profile"]) != (
        photo["content_version"], photo["thumbnail_profile"]
    ):
        raise PhotographyError("PHOTO_CHANGED", "Photo or preview changed during analysis; retry with its current version.")
    if hashlib.sha256(read_preview(current, config, store)).hexdigest() != image_hash:
        raise PhotographyError("PHOTO_CHANGED", "Preview content changed during analysis; the result was not saved.")
    return current


def make_plan(ids, store, config, provider, force):
    profile = provider.profile()
    config_id = fingerprint(profile)
    entries = []
    for photo_id in ids:
        try:
            _, preview, image_hash, key = prepare(photo_id, store, config, config_id)
            cached = None if force else store.cached_analysis(photo_id, key)
            entry = {"photo_id": photo_id, "status": "cached" if cached else "pending",
                     "input_image_hash": image_hash, "preview_bytes": len(preview)}
            if cached:
                entry["analysis_id"] = cached["analysis_id"]
        except PhotographyError as exc:
            entry = {"photo_id": photo_id, "status": "failed", "error": exc.to_dict()}
        entries.append(entry)
    pending = sum(item["status"] == "pending" for item in entries)
    failures = sum(item["status"] == "failed" for item in entries)
    result = {"dry_run": True, "status": "ready" if not failures else "partial",
              "requested": len(ids), "pending": pending,
              "cached": len(ids) - pending - failures, "failed": failures,
              "analysis_config_id": config_id, "analysis_profile": profile, "results": entries}
    if failures == len(ids):
        result["status"] = "failed"
    if pending:
        try:
            provider.check_ready()
        except PhotographyError as exc:
            result.update(status="blocked", error=exc.to_dict())
    return result


def analyze(photo_ids, *, force=False, config: Config | None = None,
            provider: VisionProvider | None = None, storage: AnalysisStorage | None = None,
            dry_run=False) -> dict:
    ids = unique_ids(photo_ids)
    if type(force) is not bool or type(dry_run) is not bool:
        raise PhotographyError("INVALID_ARGUMENT", "force and dry_run must be booleans.")
    config = config or Config.from_env()
    provider = provider or OpenAIResponsesProvider()
    try:
        with nullcontext(storage) if storage is not None else SQLiteStorage(config.state_dir) as store:
            plan = make_plan(ids, store, config, provider, force)
            if dry_run:
                return plan
            if plan["status"] == "blocked":
                raise PhotographyError(plan["error"]["code"], plan["error"]["message"])
            run = {"run_id": f"run_{uuid4().hex}", "status": "running", "started_at": now(),
                   "completed_at": None, "requested": len(ids), "analyzed": 0, "cached": 0,
                   "photo_ids": ids,
                   "failed": 0, "results": [], "analysis_config_id": plan["analysis_config_id"],
                   "analysis_profile": plan["analysis_profile"], "force": force}
            with store.transaction():
                store.save_analysis_run(run)
            fatal_error = None
            interrupted = False
            for photo_id in ids:
                photo_started = time.perf_counter()
                record = None
                try:
                    if fatal_error:
                        raise PhotographyError(fatal_error["code"], fatal_error["message"])
                    photo, preview, image_hash, cache_key = prepare(photo_id, store, config, plan["analysis_config_id"])
                    cached = None if force else store.cached_analysis(photo_id, cache_key)
                    if cached:
                        entry = {"photo_id": photo_id, "status": "cached", "analysis_id": cached["analysis_id"]}
                    else:
                        # No database transaction spans this potentially slow network request.
                        model_started = time.perf_counter()
                        output = provider.analyze(preview)
                        model_elapsed = round(time.perf_counter() - model_started, 3)
                        data = validate_analysis(output.data)
                        record = {"analysis_id": f"analysis_{uuid4().hex}", "photo_id": photo_id,
                                  "content_version": photo["content_version"], "input_image_hash": image_hash,
                                  "thumbnail_profile": photo["thumbnail_profile"], "cache_key": cache_key,
                                  "analysis_config_id": plan["analysis_config_id"],
                                  "analysis_profile": plan["analysis_profile"], "model": output.model,
                                  "response_id": output.response_id, "usage": output.usage,
                                  "model_source": output.model_source, "usage_scope": output.usage_scope,
                                  "model_elapsed_seconds": model_elapsed,
                                  "created_at": now(), "data": data}
                        entry = {"photo_id": photo_id, "status": "analyzed", "analysis_id": record["analysis_id"],
                                 "model_elapsed_seconds": model_elapsed, "usage": output.usage,
                                 "usage_scope": output.usage_scope}
                    entry["elapsed_seconds"] = round(time.perf_counter() - photo_started, 3)
                    next_run = checkpoint(run, entry)
                    with store.transaction():
                        current = assert_current(photo, image_hash, store, config)
                        if record:
                            store.put_analysis(record)
                        store.put_photo(dict(current, needs_analysis=False))
                        store.save_analysis_run(next_run)
                    run = next_run
                    continue
                except KeyboardInterrupt:
                    interrupted = True
                    fatal_error = {"code": "INTERRUPTED", "message": "Analysis was interrupted; completed photos remain saved."}
                    error = fatal_error
                except PhotographyError as exc:
                    error = exc.to_dict()
                    if isinstance(exc, ProviderError) and exc.fatal:
                        fatal_error = error
                entry = {"photo_id": photo_id, "status": "failed", "error": error,
                         "elapsed_seconds": round(time.perf_counter() - photo_started, 3)}
                next_run = checkpoint(run, entry)
                with store.transaction():
                    store.save_analysis_run(next_run)
                run = next_run
            run["status"] = "completed" if not run["failed"] else "failed" if run["failed"] == len(ids) else "partial"
            run["completed_at"] = now()
            if interrupted:
                run["interrupted"] = True
            with store.transaction():
                store.save_analysis_run(run)
            return run
    except sqlite3.Error:
        raise PhotographyError("STORAGE_UNAVAILABLE", "Analysis database could not be read or written. Completed checkpoints remain queryable.") from None


def checkpoint(run, entry):
    count = entry["status"]
    return {**run, count: run[count] + 1, "results": [*run["results"], entry]}
