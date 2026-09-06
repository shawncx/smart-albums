"""Exact search of one local vector space, with scoped coverage and stable results."""
from __future__ import annotations

import math
import time
from uuid import uuid4

from .analysis_schema import fingerprint
from .config import PhotographyError
from .embedding_model import validate_vector
from .embeddings import STATES, inspect_embedding
from .sqlite_storage import now


def search(query, *, store, encoder, album_id=None, library_id=None, limit=10, analysis_config=None):
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise PhotographyError("INVALID_ARGUMENT", "Search query must contain 1 to 2000 characters.")
    if not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise PhotographyError("INVALID_ARGUMENT", "Search limit must be between 1 and 1000.")
    started = time.perf_counter()
    query = query.strip()
    profile = encoder.profile()
    encoder_id = fingerprint(profile)
    candidates = []
    counts = {s: 0 for s in STATES}
    # A read snapshot keeps the photos, analyses and vectors consistent without locking out readers.
    with store.read_snapshot():
        photos = store.search_photos(album_id=album_id, library_id=library_id)
        records = store.embedding_candidates(encoder_id, album_id=album_id, library_id=library_id)
        for photo in photos:
            entry, document, vector = inspect_embedding(photo, store, profile, records.get(photo["photo_id"]), analysis_config=analysis_config)
            counts[entry["status"]] += 1
            if vector is not None:
                candidates.append((photo, document, vector))
    result = {"search_id": "search_" + uuid4().hex, "created_at": now(), "query": query,
              "encoder_id": encoder_id, "album_id": album_id, "library_id": library_id,
              "coverage": {**counts, "total": len(photos)}, "limit": limit, "results": [],
              "local_model_calls": 0, "visual_model_calls": 0,
              "scope_note": "Saved indexed descriptions; originals not accessed. Cosine scores are not probabilities."}
    if candidates:
        encoded = encoder.encode(query, query=True)
        query_vector = validate_vector(encoded.vector, profile["dimensions"])
        rank_started = time.perf_counter()
        ranked = []
        for photo, document, vector in candidates:
            score = max(-1., min(1., math.fsum(a * b for a, b in zip(query_vector, vector))))
            ranked.append({"photo_id": photo["photo_id"], "relative_path": photo["relative_path"],
                           "score": score, "description": document["description"], "analysis_id": document["analysis_id"],
                           "content_version": document["content_version"], "thumbnail_id": photo["photo_id"]})
        ranked.sort(key=lambda item: (-item["score"], item["photo_id"]))
        result.update(results=ranked[:limit], local_model_calls=1, query_encoding_seconds=encoded.elapsed_seconds,
                      ranking_seconds=time.perf_counter() - rank_started, query_token_count=encoded.token_count)
    result.update(elapsed_seconds=time.perf_counter() - started, model_load_seconds=getattr(encoder, "load_seconds", 0))
    return result


def add_search_selection(snapshot, photo_ids, *, store, album_name):
    """Only add explicit IDs from the displayed snapshot. Never repeat the search."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("results"), list):
        raise PhotographyError("INVALID_ARGUMENT", "Use a saved search JSON result.")
    allowed = {r.get("photo_id") for r in snapshot["results"] if isinstance(r, dict)}
    if not photo_ids or not all(isinstance(i, str) and i in allowed for i in photo_ids):
        raise PhotographyError("INVALID_ARGUMENT", "Select non-empty photo IDs from this search result.")
    with store.transaction():
        album = store.create_album(album_name)
        result = store.change_members(album["album_id"], photo_ids)
    return {**result, "album_name": album["name"], "search_id": snapshot.get("search_id"),
            "visual_model_calls": 0, "local_model_calls": 0}
