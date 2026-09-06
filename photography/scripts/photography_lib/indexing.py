"""Confirmed, resumable indexing of saved SQLite previews, never original files."""
from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import time
from uuid import uuid4

from .fingerprints import fingerprint
from .config import PhotographyError
from .image_vectors import pack_vector, unpack_vector, validate_vector
from .index_lock import execution_lock
from .index_storage import profile_identity, timestamp
from .thumbnails import MAX_PREVIEW_BYTES, stored_preview


COVERAGE_STATUSES = ("ready", "missing", "stale", "invalid_input", "invalid_vector")
ITEM_STATUSES = ("pending", "running", "indexed", "cached", "failed", "stale", "invalid_input")
INPUT_FIELDS = ("photo_id", "content_version", "thumbnail_profile", "input_image_hash")


def resolve_profile(store, profile_id=None):
    selected = profile_id if profile_id is not None else store.default_index_profile()
    if not selected:
        raise PhotographyError("INDEX_CONFIGURATION_REQUIRED",
            "No image-index profile is configured. Run index setup, then index configure --default-profile <id>.")
    return store.index_profile(selected)


def _dimensions(profile):
    profile_identity(profile)
    dimensions = profile.get("dimensions")
    if (type(dimensions) is not int or not 1 <= dimensions <= 4096
            or profile.get("dtype", "float32-le") not in ("float32-le", "float32")
            or profile.get("byte_order", "little") != "little" or profile.get("normalized", True) is not True):
        raise PhotographyError("INDEX_PROFILE_INVALID",
            "Image indexes require 1–4096 dimensions, normalized vectors and float32-le storage.")
    return dimensions


def _preview_input(photo, store, *, check_preview):
    if photo.get("state") == "error":
        raise PhotographyError("INDEX_INVALID_INPUT", "Ingestion recorded an error; repair or rescan this photo first.")
    if photo.get("state") not in ("available", "missing"):
        raise PhotographyError("INDEX_INVALID_INPUT", "Photo has no valid ingestion state.")
    if any(not isinstance(photo.get(key), str) or not photo[key]
           for key in ("photo_id", "content_version", "thumbnail_profile")):
        raise PhotographyError("INVALID_PREVIEW", "Stored photo has incomplete preview identity; rescan to repair it.")
    thumbnail = store.thumbnail(photo["photo_id"], include_data=False)
    if (thumbnail["content_version"], thumbnail["profile"]) != (
            photo["content_version"], photo["thumbnail_profile"]):
        raise PhotographyError("INVALID_PREVIEW", "Stored preview does not match the photo version.")
    if (thumbnail.get("mime_type") != "image/jpeg"
            or any(type(thumbnail.get(key)) is not int or not 1 <= thumbnail[key] <= 4096 for key in ("width", "height"))
            or type(thumbnail.get("size_bytes")) is not int
            or not 0 < thumbnail["size_bytes"] <= MAX_PREVIEW_BYTES
            or not isinstance(thumbnail.get("image_hash"), str)
            or re.fullmatch(r"[0-9a-f]{64}", thumbnail["image_hash"]) is None):
        raise PhotographyError("INVALID_PREVIEW", "Stored preview metadata is invalid; rescan to repair it.")
    snapshot = {
        "photo_id": photo["photo_id"], "content_version": photo["content_version"],
        "thumbnail_profile": photo["thumbnail_profile"], "input_image_hash": thumbnail["image_hash"],
    }
    data = None
    if check_preview:
        data = stored_preview(photo, store)
        if len(data) != thumbnail["size_bytes"] or hashlib.sha256(data).hexdigest() != thumbnail["image_hash"]:
            raise PhotographyError("INVALID_PREVIEW", "Stored preview changed or has incorrect byte metadata.")
    return snapshot, data


def _inspect_result(snapshot, store, profile):
    profile_id = profile_identity(profile)[0]
    record = store._index_result(snapshot, profile_id)
    if record is None:
        stale = store._has_index_results(snapshot["photo_id"], profile_id)
        return ("stale" if stale else "missing", "input_changed" if stale else "not_indexed", None, None)
    try:
        if (record["dimensions"] != _dimensions(profile) or record["dtype"] != "float32-le"
                or record["normalized"] != 1 or not isinstance(record["vector"], bytes)
                or hashlib.sha256(record["vector"]).hexdigest() != record["vector_hash"]):
            raise ValueError("Vector metadata or checksum mismatch.")
        vector = unpack_vector(record["vector"], record["dimensions"])
        validate_vector(vector, record["dimensions"], normalized=True)
    except (PhotographyError, ValueError, TypeError, OverflowError) as exc:
        return "invalid_vector", str(exc), record, None
    return "ready", "matching_saved_input", record, vector


def inspect_index(photo, store, profile, *, check_preview=False):
    _dimensions(profile)
    entry = {
        "photo_id": photo["photo_id"], "input_scope": "stored_preview", "original_checked": False,
        "preview_integrity": "verified" if check_preview else "unchecked",
    }
    try:
        snapshot, _ = _preview_input(photo, store, check_preview=check_preview)
    except PhotographyError as exc:
        entry.update(status="invalid_input", reason=str(exc), error=exc.to_dict(),
                     preview_integrity="failed" if check_preview else "unchecked")
        return entry, None, None
    status, reason, record, vector = _inspect_result(snapshot, store, profile)
    entry.update(snapshot, status=status, reason=reason)
    if record:
        entry["result_id"] = record["result_id"]
    return entry, record, vector


def index_status(photos, store, profile):
    counts = dict.fromkeys(COVERAGE_STATUSES, 0)
    items = []
    for photo in sorted(photos, key=lambda item: item["photo_id"]):
        entry, _, _ = inspect_index(photo, store, profile)
        counts[entry["status"]] += 1
        items.append(entry)
    return {
        "profile_id": profile_identity(profile)[0], "counts": {"total": len(items), **counts},
        "items": items, "model_calls": 0, "preview_integrity": "unchecked",
        "original_checked": False, "input_scope": "stored_preview",
    }


def _plan_digest(plan):
    return fingerprint({key: plan[key] for key in
        ("version", "run_id", "profile_id", "profile", "snapshots")})


def create_plan(photo_ids, *, store, config, profile, persist=True, check_ready=None):
    if (not isinstance(photo_ids, (list, tuple)) or not photo_ids
            or any(not isinstance(value, str) or not value for value in photo_ids)):
        raise PhotographyError("INVALID_ARGUMENT", "Provide an explicit, nonempty list of photo IDs.")
    _dimensions(profile)
    # Detach the persisted identity from mutable adapter/caller dictionaries.
    import json
    profile = json.loads(profile_identity(profile)[1])
    snapshots = []
    counts = dict.fromkeys(COVERAGE_STATUSES, 0)
    with store.read_snapshot():
        for photo_id in sorted(set(photo_ids)):
            photo = store.photo(photo_id)
            entry, _, _ = inspect_index(photo, store, profile, check_preview=True)
            counts[entry["status"]] += 1
            entry["action"] = ("reuse" if entry["status"] == "ready" else
                               "skip" if entry["status"] == "invalid_input" else "encode")
            for key in ("content_version", "thumbnail_profile"):
                entry.setdefault(key, photo.get(key))
            snapshots.append(entry)
    pending = sum(item["action"] == "encode" for item in snapshots)
    plan = {
        "version": "image-index-plan-v1", "run_id": "index_" + uuid4().hex,
        "profile_id": profile_identity(profile)[0], "profile": profile,
        "status": "proposed" if persist else "dry_run", "created_at": timestamp(),
        "snapshots": snapshots,
        "counts": {"total": len(snapshots), **counts, "pending": pending, "cached": counts["ready"]},
        "pending": pending,
        "cached": counts["ready"], "model_calls": 0,
        "input_scope": "stored_preview", "original_checked": False,
        "preview_integrity": "failed" if counts["invalid_input"] else "verified",
    }
    plan["digest"] = _plan_digest(plan)
    if pending and check_ready is not None:
        if not callable(check_ready):
            raise PhotographyError("INVALID_ARGUMENT", "The image-index readiness check must be callable.")
        check_ready()
    if persist:
        with store.transaction():
            store.put_index_profile(profile)
            store._create_index_run(plan)
    return {**plan, "items": snapshots}


def job(store, run_id):
    run = store._index_run(run_id)
    plan = run["plan"]
    items = store._index_items(run_id)
    try:
        if (plan["version"] != "image-index-plan-v1" or plan["run_id"] != run_id
                or _plan_digest(plan) != run["digest"] or plan["digest"] != run["digest"]
                or profile_identity(plan["profile"])[0] != run["profile_id"]
                or plan["profile_id"] != run["profile_id"]
                or store.index_profile(run["profile_id"]) != plan["profile"]
                or run["confirmed_digest"] not in (None, run["digest"])):
            raise ValueError("Plan identity mismatch.")
        snapshots = plan["snapshots"]
        if [item["photo_id"] for item in items] != [item["photo_id"] for item in snapshots]:
            raise ValueError("Plan scope mismatch.")
        for item, snapshot in zip(items, snapshots):
            if (item["snapshot"] != snapshot or item["profile_id"] != plan["profile_id"]
                    or item["action"] != snapshot["action"]
                    or any(item[key] != snapshot.get(key) for key in INPUT_FIELDS)
                    or item["status"] not in ITEM_STATUSES
                    or not isinstance(item["attempts"], list)):
                raise ValueError("Plan item mismatch.")
    except (ValueError, TypeError, KeyError) as exc:
        raise PhotographyError("INDEX_PLAN_INVALID", "Saved plan or input scope failed its integrity check.") from exc
    counts = {"total": len(items), **dict.fromkeys(ITEM_STATUSES, 0)}
    for item in items:
        counts[item["status"]] += 1
    summary = {
        "counts": counts, "model_calls": run["model_calls"],
        "attempt_count": sum(len(item["attempts"]) for item in items),
    }
    return {
        **plan, "status": run["status"], "confirmed": run["confirmed_digest"] == run["digest"],
        "confirmed_at": run["confirmed_at"], "updated_at": run["updated_at"],
        "items": items, "results": items, "planned_counts": plan["counts"], "counts": counts,
        "model_calls": run["model_calls"], "summary": summary,
    }


def _current_input(item, store):
    photo = store.photo(item["photo_id"])
    snapshot = item["snapshot"]
    if (photo.get("content_version"), photo.get("thumbnail_profile")) != (
            snapshot.get("content_version"), snapshot.get("thumbnail_profile")):
        raise PhotographyError("INDEX_INPUT_CHANGED", "Photo changed after planning; create a new plan for its new input.")
    current, data = _preview_input(photo, store, check_preview=True)
    if any(current[key] != snapshot.get(key) for key in INPUT_FIELDS):
        raise PhotographyError("INDEX_INPUT_CHANGED", "Saved preview changed after planning; create a new plan.")
    return current, data


def _finish_item(store, run_id, item, status, *, result_id=None, error=None):
    item.update(status=status, result_id=result_id, error=error)
    if item["attempts"] and not item["attempts"][-1].get("completed_at"):
        item["attempts"][-1].update(status=status, completed_at=timestamp())
        if error:
            item["attempts"][-1]["error"] = error
    store._update_index_item(run_id, item)
    store._release_index_input(run_id, item["photo_id"])


def _check_encoder(encoder, profile):
    if profile_identity(encoder.profile())[0] != profile_identity(profile)[0]:
        raise PhotographyError("INDEX_PROFILE_MISMATCH", "Encoder identity does not match this immutable plan.")


def execute_plan(run_id, *, store, config, encoder, confirm=None, resume=False):
    if resume and confirm is not None:
        raise PhotographyError("INVALID_ARGUMENT", "Resume uses an existing confirmation; it cannot grant a new one.")
    calls = 0
    with execution_lock(store.index_lock_path):
        with store.transaction():
            saved = job(store, run_id)
            if confirm is not None and confirm != saved["digest"]:
                raise PhotographyError("INDEX_CONFIRMATION_MISMATCH", "Confirmation does not match the saved plan digest.")
            if resume and saved["pending"] and not saved["confirmed"]:
                raise PhotographyError("INDEX_CONFIRMATION_REQUIRED", "First execute and confirm this saved plan; resume cannot confirm it.")
            if saved["pending"] and not (saved["confirmed"] or confirm == saved["digest"]):
                raise PhotographyError("INDEX_CONFIRMATION_REQUIRED", "Explicitly confirm this saved plan digest before inference.")
            if not resume and saved["status"] != "proposed":
                if saved["status"] == "completed":
                    return {**saved, "model_calls_this_execution": 0}
                raise PhotographyError("INDEX_RESUME_REQUIRED", "This plan has already started. Use index resume to retry its saved scope.")
            if confirm is not None:
                store._update_index_run(run_id, confirmed_digest=confirm)
            store._recover_index_claims()
            store._update_index_run(run_id, status="running")
            saved = job(store, run_id)
        profile = saved["profile"]
        dimensions = _dimensions(profile)
        for item in saved["items"]:
            encoding_stage = False
            attempt_started = False
            try:
                with store.transaction():
                    if item["action"] == "skip":
                        error = item["snapshot"].get("error") or {
                            "code": "INDEX_INVALID_INPUT", "message": item["snapshot"]["reason"]}
                        _finish_item(store, run_id, item, "invalid_input", error=error)
                        continue
                    current, data = _current_input(item, store)
                    status, _, record, _ = _inspect_result(current, store, profile)
                    if status == "ready":
                        state = "indexed" if item["status"] == "indexed" else "cached"
                        _finish_item(store, run_id, item, state, result_id=record["result_id"])
                        continue
                    if item["action"] != "encode":
                        raise PhotographyError("INDEX_REPLAN_REQUIRED",
                            "A planned cache hit is no longer valid. Create and confirm a repair plan before inference.")
                    store._claim_index_input(run_id, current, saved["profile_id"])
                    item["attempts"].append({
                        "attempt": len(item["attempts"]) + 1, "started_at": timestamp(),
                        "status": "running", "model_called": False,
                    })
                    attempt_started = True
                    item.update(status="running", result_id=None, error=None)
                    store._update_index_item(run_id, item)
                encoding_stage = True
                _check_encoder(encoder, profile)
                with store.transaction():
                    _, data = _current_input(item, store)
                    item["attempts"][-1]["model_called"] = True
                    store._update_index_item(run_id, item)
                    store._update_index_run(run_id, model_call=True)
                calls += 1
                started = time.perf_counter()
                encoded = encoder.encode_image(data)
                item["attempts"][-1]["call_elapsed_seconds"] = time.perf_counter() - started
                _check_encoder(encoder, profile)
                validate_vector(encoded.vector, dimensions, normalized=True)
                blob = pack_vector(encoded.vector, dimensions)
                elapsed = getattr(encoded, "elapsed_seconds", None)
                if isinstance(elapsed, (float, int)) and not isinstance(elapsed, bool) and math.isfinite(elapsed) and elapsed >= 0:
                    item["attempts"][-1]["image_encode_seconds"] = elapsed
                token_count = getattr(encoded, "token_count", None)
                if type(token_count) is int and token_count >= 0:
                    item["attempts"][-1]["token_count"] = token_count
                encoding_stage = False
                with store.transaction():
                    current, _ = _current_input(item, store)
                    status, _, record, _ = _inspect_result(current, store, profile)
                    if status == "ready":
                        result_id, state = record["result_id"], "cached"
                    else:
                        result_id = store._put_index_result(
                            current, saved["profile_id"], blob, hashlib.sha256(blob).hexdigest(), dimensions)
                        state = "indexed"
                    _finish_item(store, run_id, item, state, result_id=result_id)
            except (PhotographyError, sqlite3.Error, OSError) as exc:
                error = exc if isinstance(exc, PhotographyError) else PhotographyError("INDEX_EXECUTION_FAILED", str(exc))
                state = ("stale" if error.code == "INDEX_INPUT_CHANGED" else
                         "invalid_input" if error.code in ("INVALID_PREVIEW", "INDEX_INVALID_INPUT") else "failed")
                if attempt_started:
                    item["attempts"][-1].update(status=state, completed_at=timestamp(), error=error.to_dict())
                with store.transaction():
                    _finish_item(store, run_id, item, state, error=error.to_dict())
                # Installation/configuration/resource failures must not cause a per-photo retry storm.
                if encoding_stage:
                    break
        with store.transaction():
            result = job(store, run_id)
            counts = result["counts"]
            successes = counts["indexed"] + counts["cached"]
            state = "completed" if successes == counts["total"] else "partial" if successes else "failed"
            store._update_index_run(run_id, status=state)
        return {**job(store, run_id), "model_calls_this_execution": calls}
