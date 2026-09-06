"""Read-only metadata browsing and exact, single-profile image retrieval."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
import time
import unicodedata
from uuid import uuid4

from .fingerprints import fingerprint
from .config import PhotographyError


SCHEMA = "management-snapshot-v1"
STATUSES = ("ready", "missing", "stale", "invalid_input", "invalid_vector")


def _limit(value):
    if type(value) is not int or not 1 <= value <= 1000:
        raise PhotographyError("INVALID_ARGUMENT", "Limit must be an integer between 1 and 1000.")
    return value


def _cursor(after):
    if not isinstance(after, str):
        raise PhotographyError("INVALID_ARGUMENT", "The pagination cursor must be a photo or album ID.")
    return after


def _scope(album_id, library_id):
    if album_id is not None and library_id is not None:
        raise PhotographyError("INVALID_ARGUMENT", "Use --album-id or --library-id, not both.")
    for value in (album_id, library_id):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise PhotographyError("INVALID_ARGUMENT", "Scope IDs must be nonempty strings.")
    return {"album_id": album_id, "library_id": library_id}


def _query(query):
    if not isinstance(query, str) or not query.strip():
        raise PhotographyError("INVALID_ARGUMENT", "Search text must not be blank.")
    return query


def _fold(text):
    return unicodedata.normalize("NFC", text).casefold()


def _profile(store, profile_id, *, required=False):
    from .indexing import resolve_profile

    if profile_id is None and store.default_index_profile() is None and not required:
        return None
    return resolve_profile(store, profile_id)


def _snapshot(view, target, profile, scope=None, *, mode="metadata"):
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "snapshot_id": "management_" + uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "view": view,
        "mode": mode,
        "target": target,
        "scope": scope or {"album_id": None, "library_id": None},
        "profile_id": fingerprint(profile) if profile else None,
        "index_configuration": "configured" if profile else "not_configured",
        "original_verification": "not_checked",
        "preview_integrity": "unchecked",
        "model_calls": 0,
        "image_model_calls": 0,
    }


def _page(items, id_key, limit, after):
    ordered = sorted(items, key=lambda item: item[id_key])
    remaining = [item for item in ordered if item[id_key] > after]
    selected = remaining[:limit]
    return selected, selected[-1][id_key] if len(remaining) > limit else None


def _photo_item(photo, store, profile, *, entry=None):
    item = deepcopy(photo)
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
        item["index"] = {"photo_id": photo["photo_id"], "profile_id": None, "status": "not_configured"}
    else:
        if entry is None:
            from .indexing import inspect_index

            entry, _, _ = inspect_index(photo, store, profile)
        item["index"] = {**deepcopy(entry), "profile_id": fingerprint(profile)}
    return item


def _album_item(album, store, profile):
    item = deepcopy(album)
    members = sorted(store.index_photos(album_id=album["album_id"]), key=lambda photo: photo["photo_id"])
    item["photo_count"] = len(members)
    item["cover"] = _photo_item(members[0], store, profile) if members else None
    return item


def albums(*, store, limit=100, after="", profile_id=None):
    _limit(limit)
    _cursor(after)
    with store.read_snapshot():
        profile = _profile(store, profile_id)
        all_albums = store.albums()
        selected, cursor = _page(all_albums, "album_id", limit, after)
        result = _snapshot("albums", "albums", profile)
        result.update(items=[_album_item(item, store, profile) for item in selected],
                      total=len(all_albums), limit=limit, after=after, next_cursor=cursor)
    return result


def album(album_id, *, store, limit=100, after="", profile_id=None):
    _limit(limit)
    _cursor(after)
    scope = _scope(album_id, None)
    with store.read_snapshot():
        record = store.album(album_id)
        profile = _profile(store, profile_id)
        members = store.index_photos(album_id=album_id)
        selected, cursor = _page(members, "photo_id", limit, after)
        result = _snapshot("album", "photos", profile, scope)
        result.update(album={**record, "photo_count": len(members)},
                      items=[_photo_item(item, store, profile) for item in selected],
                      total=len(members), limit=limit, after=after, next_cursor=cursor)
    return result


def photos(*, store, album_id=None, library_id=None, limit=100, after="", profile_id=None):
    _limit(limit)
    _cursor(after)
    scope = _scope(album_id, library_id)
    with store.read_snapshot():
        profile = _profile(store, profile_id)
        members = store.index_photos(**scope)
        selected, cursor = _page(members, "photo_id", limit, after)
        result = _snapshot("photos", "photos", profile, scope)
        result.update(items=[_photo_item(item, store, profile) for item in selected],
                      total=len(members), limit=limit, after=after, next_cursor=cursor)
    return result


def photo(photo_id, *, store, profile_id=None):
    with store.read_snapshot():
        record = store.photo(photo_id)
        profile = _profile(store, profile_id)
        result = _snapshot("photo", "photos", profile)
        result.update(items=[_photo_item(record, store, profile)], total=1, next_cursor=None)
    return result


def metadata_search(query, *, target, store, album_id=None, library_id=None,
                    limit=100, after="", profile_id=None):
    _query(query)
    _limit(limit)
    _cursor(after)
    scope = _scope(album_id, library_id)
    if target not in ("albums", "photos"):
        raise PhotographyError("INVALID_ARGUMENT", "Metadata search requires --target albums or photos.")
    if target == "albums" and any(value is not None for value in scope.values()):
        raise PhotographyError("INVALID_ARGUMENT", "Album-name search does not accept a photo scope.")
    needle = _fold(query)
    with store.read_snapshot():
        profile = _profile(store, profile_id)
        if target == "albums":
            candidates = store.albums()
            matches = [item for item in candidates if needle in _fold(item["name"])]
            selected, cursor = _page(matches, "album_id", limit, after)
            items = [_album_item(item, store, profile) for item in selected]
        else:
            candidates = store.index_photos(**scope)
            matches = [item for item in candidates if any(
                needle in _fold(item.get(field) or "") for field in ("relative_path", "filename")
            )]
            selected, cursor = _page(matches, "photo_id", limit, after)
            items = [_photo_item(item, store, profile) for item in selected]
        result = _snapshot("search", target, profile, scope)
        result.update(query=query, items=items, total=len(matches), scope_total=len(candidates),
                      limit=limit, after=after, next_cursor=cursor)
    return result


def semantic_search(query, *, store, config=None, album_id=None, library_id=None,
                    profile_id=None, limit=10, after=None, encoder=None, model_dir=None):
    _query(query)
    _limit(limit)
    scope = _scope(album_id, library_id)
    if after is not None:
        raise PhotographyError("INVALID_ARGUMENT", "Semantic search uses top-k ranking, not --after cursors.")
    from .indexing import inspect_index

    with store.read_snapshot():
        profile = _profile(store, profile_id, required=True)
        members = store.index_photos(**scope)
        counts = {status: 0 for status in STATUSES}
        counts["total"] = len(members)
        candidates = []
        entries = []
        for item in members:
            entry, record, vector = inspect_index(item, store, profile)
            entries.append(deepcopy(entry))
            counts[entry["status"]] += 1
            if entry["status"] == "ready":
                candidate = _photo_item(item, store, profile, entry=entry)
                candidate.update({key: record[key] for key in (
                    "result_id", "profile_id", "content_version", "thumbnail_profile", "input_image_hash"
                )})
                candidates.append((candidate, vector))
        result = _snapshot("search", "photos", profile, scope, mode="semantic")
        result.update(query=query, coverage=counts, coverage_items=entries,
                      coverage_scope="entire_selected_scope", limit=limit, results=[],
                      next_cursor=None, similarity="cosine_not_probability",
                      timings={"model_loading_seconds": None, "query_encoding_seconds": None,
                               "query_total_seconds": None, "ranking_seconds": None})

    if not candidates:
        return result

    from .image_vectors import validate_vector
    if encoder is None:
        from .index_profiles import default_model_dir
        from .siglip_embedding import SiglipEncoder

        if model_dir is None:
            if config is None:
                raise PhotographyError("INVALID_ARGUMENT", "Semantic search requires config, model_dir or an injected encoder.")
            model_dir = default_model_dir(config.state_dir)
        encoder = SiglipEncoder(model_dir, profile=profile)
    if fingerprint(encoder.profile()) != result["profile_id"]:
        raise PhotographyError("INDEX_PROFILE_MISMATCH", "Query encoder does not match the selected index profile.")
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
