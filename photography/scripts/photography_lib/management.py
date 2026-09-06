"""Single-album browsing, exact semantic retrieval and explicit path maintenance."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
import sqlite3
import time
import unicodedata
from uuid import uuid4

from .config import PhotographyError
from .fingerprints import fingerprint
from .source_paths import photo_filename


SCHEMA = "album-snapshot-v1"
STATUSES = ("ready", "missing", "stale", "invalid_input", "invalid_vector")


def _limit(value):
    if type(value) is not int or not 1 <= value <= 1000:
        raise PhotographyError("INVALID_ARGUMENT", "Limit must be an integer between 1 and 1000.")
    return value


def _cursor(after):
    if not isinstance(after, str):
        raise PhotographyError("INVALID_ARGUMENT", "The pagination cursor must be a photo ID.")
    return after


def _query(query):
    if not isinstance(query, str) or not query.strip():
        raise PhotographyError("INVALID_ARGUMENT", "Search text must not be blank.")
    return query


def _fold(text):
    return unicodedata.normalize("NFC", text).casefold()


def _profile(store, profile_id, *, required=False):
    from .image_embedding import resolve_profile

    if profile_id is None and store.default_embedding_profile() is None and not required:
        return None
    return resolve_profile(store, profile_id)


def _snapshot(view, store, profile, *, mode="metadata"):
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "snapshot_id": "album_snapshot_" + uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "album": store.album(),
        "view": view,
        "mode": mode,
        "component": "image_embedding",
        "profile_id": fingerprint(profile) if profile else None,
        "embedding_configuration": "configured" if profile else "not_configured",
        "original_verification": "not_checked",
        "preview_integrity": "unchecked",
        "model_calls": 0,
        "image_model_calls": 0,
    }


def _page(items, limit, after):
    ordered = sorted(items, key=lambda item: item["photo_id"])
    remaining = [item for item in ordered if item["photo_id"] > after]
    selected = remaining[:limit]
    return selected, selected[-1]["photo_id"] if len(remaining) > limit else None


def _photo_item(photo, store, profile, *, entry=None):
    item = deepcopy(photo)
    item["filename"] = photo_filename(photo)
    item["thumbnail_id"] = photo["photo_id"]
    item["preview_integrity"] = "unchecked"
    item["original_verification"] = "not_checked"
    item["input_image_hash"] = None
    try:
        thumbnail = store.thumbnail(photo["photo_id"], include_data=False)
        item["thumbnail"] = thumbnail
        if (thumbnail["content_version"], thumbnail["profile"]) != (
            photo.get("content_version"), photo.get("thumbnail_profile")
        ):
            raise PhotographyError("INVALID_PREVIEW", "Stored preview does not match this photo version.")
        item["input_image_hash"] = thumbnail["image_hash"]
    except PhotographyError as exc:
        item["preview_error"] = exc.to_dict()
    if profile is None:
        item["image_embedding"] = {
            "photo_id": photo["photo_id"], "component": "image_embedding",
            "profile_id": None, "status": "not_configured",
        }
    else:
        if entry is None:
            from .image_embedding import inspect_embedding

            entry, _, _ = inspect_embedding(photo, store, profile)
        item["image_embedding"] = {**deepcopy(entry), "profile_id": fingerprint(profile)}
    return item


def album_info(*, store, view="open", profile_id=None):
    from .image_embedding import embedding_status

    with store.read_snapshot():
        profile = _profile(store, profile_id)
        members = store.photos()
        result = _snapshot(view, store, profile)
        coverage = (embedding_status(members, store, profile)["counts"] if profile else
                    {"total": len(members), "not_configured": len(members)})
        result.update(photo_count=len(members), coverage=coverage, coverage_scope="entire_album",
                      items=[], total=len(members), next_cursor=None)
        result["album"]["photo_count"] = len(members)
    return result


def photos(*, store, limit=100, after="", profile_id=None):
    _limit(limit)
    _cursor(after)
    with store.read_snapshot():
        profile = _profile(store, profile_id)
        members = store.photos()
        selected, cursor = _page(members, limit, after)
        result = _snapshot("photos", store, profile)
        result.update(items=[_photo_item(item, store, profile) for item in selected],
                      total=len(members), limit=limit, after=after, next_cursor=cursor)
    return result


def photo(photo_id, *, store, profile_id=None):
    with store.read_snapshot():
        record = store.photo(photo_id)
        profile = _profile(store, profile_id)
        result = _snapshot("photo", store, profile)
        result.update(items=[_photo_item(record, store, profile)], total=1, next_cursor=None)
    return result


def metadata_search(query, *, store, limit=100, after="", profile_id=None):
    _query(query)
    _limit(limit)
    _cursor(after)
    needle = _fold(query)
    with store.read_snapshot():
        profile = _profile(store, profile_id)
        candidates = store.photos()
        matches = [item for item in candidates if any(needle in _fold(text or "") for text in (
            photo_filename(item), item["original_absolute_path"], item["original_relative_path"]
        ))]
        selected, cursor = _page(matches, limit, after)
        result = _snapshot("search", store, profile)
        result.update(query=query, items=[_photo_item(item, store, profile) for item in selected],
                      total=len(matches), album_total=len(candidates),
                      limit=limit, after=after, next_cursor=cursor)
    return result


def semantic_search(query, *, store, config=None, profile_id=None, limit=10, after=None, encoder=None):
    _query(query)
    _limit(limit)
    if after is not None:
        raise PhotographyError("INVALID_ARGUMENT", "Semantic search uses top-k ranking, not --after cursors.")
    from .image_embedding import inspect_embedding

    with store.read_snapshot():
        profile = _profile(store, profile_id, required=True)
        members = sorted(store.photos(), key=lambda item: item["photo_id"])
        counts = {status: 0 for status in STATUSES}
        counts["total"] = len(members)
        candidates = []
        entries = []
        for item in members:
            entry, record, vector = inspect_embedding(item, store, profile)
            entries.append(deepcopy(entry))
            counts[entry["status"]] += 1
            if entry["status"] == "ready":
                candidate = _photo_item(item, store, profile, entry=entry)
                candidate.update({key: record[key] for key in (
                    "result_id", "profile_id", "content_version", "thumbnail_profile", "input_image_hash"
                )})
                candidates.append((candidate, vector))
        result = _snapshot("search", store, profile, mode="semantic")
        result.update(query=query, coverage=counts, coverage_items=entries,
                      coverage_scope="entire_album", limit=limit, results=[],
                      next_cursor=None, similarity="cosine_not_probability",
                      timings={"model_loading_seconds": None, "query_encoding_seconds": None,
                               "query_total_seconds": None, "ranking_seconds": None})

    if not candidates:
        return result

    from .image_vectors import validate_vector
    if encoder is None:
        from .image_embedding_profiles import default_model_dir
        from .siglip_embedding import SiglipEncoder

        if config is None:
            raise PhotographyError("INVALID_ARGUMENT", "Semantic search requires config or an injected encoder.")
        encoder = SiglipEncoder(default_model_dir(config.model_cache_root), profile=profile)
    if fingerprint(encoder.profile()) != result["profile_id"]:
        raise PhotographyError("INDEX_PROFILE_MISMATCH", "Query encoder does not match the selected image-embedding profile.")
    started = time.perf_counter()
    encoding = encoder.encode_text(query)
    query_vector = validate_vector(encoding.vector, profile["dimensions"])
    result["timings"]["query_total_seconds"] = time.perf_counter() - started
    result["timings"]["query_encoding_seconds"] = encoding.elapsed_seconds
    result["timings"]["model_loading_seconds"] = getattr(encoder, "load_seconds", None)
    result["model_calls"] = 1
    result["query_token_count"] = encoding.token_count

    started = time.perf_counter()
    query_norm = math.sqrt(math.fsum(value * value for value in query_vector))
    ranked = []
    for item, vector in candidates:
        norm = math.sqrt(math.fsum(value * value for value in vector))
        score = math.fsum(a * b for a, b in zip(query_vector, vector)) / (query_norm * norm)
        ranked.append({**item, "score": max(-1.0, min(1.0, score))})
    ranked.sort(key=lambda item: (-item["score"], item["photo_id"]))
    result["results"] = ranked[:limit]
    result["timings"]["ranking_seconds"] = time.perf_counter() - started
    return result


def _persist_path(record, resolution, album, store):
    from .source_paths import persist_resolution
    from .sqlite_storage import SQLiteStorage

    try:
        with SQLiteStorage.open(store.database_path, writable=True) as writer:
            if writer.album()["id"] != album["id"]:
                raise PhotographyError("ALBUM_MISMATCH", "The database now contains a different album.")
            return persist_resolution(writer, record, resolution)
    except (PhotographyError, OSError, sqlite3.Error) as exc:
        cause = exc.to_dict() if isinstance(exc, PhotographyError) else {
            "code": "STORAGE_UNAVAILABLE", "message": str(exc),
        }
        raise PhotographyError(
            cause["code"], "Original-file resolution was not saved: " + str(exc),
            details={"album": album, "photo_id": record["photo_id"], "resolution": resolution,
                     "persisted": False, "cause": cause},
        ) from exc


def original(photo_id, *, store):
    from .source_paths import resolve_original

    with store.read_snapshot():
        record, album = store.photo(photo_id), store.album()
    resolution = resolve_original(record, store.database_path)
    persist = resolution["repair_required"] or resolution["status"] != record["original_status"]
    updated = _persist_path(record, resolution, album, store) if persist else record
    if resolution["status"] == "unavailable":
        error = resolution.get("error") or {
            "code": "ORIGINAL_UNAVAILABLE", "message": "Original file could not be accessed.",
        }
        raise PhotographyError(error["code"], error["message"], details={
            "album": album, "photo_id": photo_id, "resolution": resolution, "persisted": persist,
        })
    return {"album": album, "photo_id": photo_id, "photo": updated,
            "status": resolution["status"], "resolution": resolution,
            "persisted": persist, "path_repaired": bool(resolution["repair_required"] and persist),
            "model_calls": 0}


def relink(photo_id, path, *, store):
    from .source_paths import relink_original

    with store.read_snapshot():
        record, album = store.photo(photo_id), store.album()
    resolution = relink_original(record, path, store.database_path)
    updated = _persist_path(record, resolution, album, store)
    return {"album": album, "photo_id": photo_id, "photo": updated,
            "status": resolution["status"], "resolution": resolution,
            "persisted": True, "path_repaired": True, "model_calls": 0}


def backup(output, *, store, config):
    from .exports import export_path

    target = export_path(output, config, store, (".sqlite", ".sqlite3", ".db"))
    return {**store.backup(target), "album": store.album(), "model_calls": 0}


def thumbnail(photo_id, output, *, store, config):
    from .exports import export_path
    from .thumbnails import stored_preview

    target = export_path(output, config, store, (".jpg", ".jpeg"))
    with store.read_snapshot():
        data = stored_preview(store.photo(photo_id), store)
        album = store.album()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"album": album, "photo_id": photo_id, "output": str(target), "bytes": len(data),
            "original_verification": "not_checked", "preview_integrity": "verified", "model_calls": 0}


def scan(scan_id, *, store):
    with store.read_snapshot():
        return {**store.scan(scan_id), "album": store.album(), "model_calls": 0}


def scan_events(scan_id, *, store, limit=100, after=0, changes_only=False):
    _limit(limit)
    if type(after) is not int or after < 0:
        raise PhotographyError("INVALID_ARGUMENT", "The event cursor must be a nonnegative integer.")
    with store.read_snapshot():
        return {**store.events(scan_id, limit, after, changes_only), "scan_id": scan_id,
                "album": store.album(), "limit": limit, "after": after,
                "changes_only": changes_only, "model_calls": 0}
