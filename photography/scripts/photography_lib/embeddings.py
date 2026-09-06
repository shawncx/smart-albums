"""Incremental embeddings of saved, current observations. No vision provider calls."""
from __future__ import annotations

import hashlib
import sqlite3
import time

from .analysis_schema import fingerprint
from .config import PhotographyError
from .embedding_model import pack_vector, unpack_vector
from .embedding_text import RECIPE_VERSION, retrieval_text, text_hash
from .sqlite_storage import now
from .status import saved_observation

STATES = ("ready", "missing", "stale", "needs_analysis", "invalid")


def current_document(photo, store, analysis_config=None):
    entry, observation = saved_observation(photo, store, analysis_config, check_source=False)
    # saved_observation may return historical data for display. Never index that fallback.
    if not entry["analysis_id"]:
        return None, entry["reason"] or "never_analyzed"
    text = retrieval_text(observation["data"])
    return {"photo_id": photo["photo_id"], "analysis_id": observation["analysis_id"],
            "content_version": photo["content_version"], "text_hash": text_hash(text),
            "recipe_version": RECIPE_VERSION, "text": text,
            "description": observation["data"]["visual_description"]}, None


def inspect_embedding(photo, store, profile, record=None, *, analysis_config=None):
    entry = {"photo_id": photo["photo_id"], "relative_path": photo["relative_path"]}
    try:
        document, reason = current_document(photo, store, analysis_config)
        if document is None:
            return {**entry, "status": "needs_analysis", "reason": reason}, None, None
        if record is None:
            return {**entry, "status": "missing", "reason": "not_embedded"}, document, None
        keys = ("analysis_id", "content_version", "text_hash", "recipe_version")
        if any(record[key] != document[key] for key in keys):
            return {**entry, "status": "stale", "reason": "saved_description_changed"}, document, None
        if (record["encoder_id"] != fingerprint(profile) or record["dimensions"] != profile["dimensions"]
                or record["dtype"] != "float32-le" or record["normalized"] != 1
                or hashlib.sha256(record["vector"]).hexdigest() != record["vector_hash"]):
            raise PhotographyError("INVALID_VECTOR", "Stored vector metadata or checksum mismatch.")
        vector = unpack_vector(record["vector"], profile["dimensions"])
        return {**entry, "status": "ready", "reason": None, "analysis_id": document["analysis_id"],
                "truncated": bool(record["truncated"])}, document, vector
    except PhotographyError as exc:
        return {**entry, "status": "invalid", "reason": exc.code, "error": exc.to_dict()}, None, None


def embedding_status(photos, store, profile, *, records=None, analysis_config=None):
    encoder_id = fingerprint(profile)
    items = [inspect_embedding(p, store, profile,
             records.get(p["photo_id"]) if records is not None else store.embedding(p["photo_id"], encoder_id),
             analysis_config=analysis_config)[0] for p in photos]
    return {"encoder_id": encoder_id, "counts": {**{s: sum(e["status"] == s for e in items) for s in STATES}, "total": len(items)},
            "items": items, "visual_model_calls": 0, "local_model_calls": 0,
            "scope_note": "Database indexed versions; originals are not accessed or verified."}


def embed(photo_ids, *, store, encoder, force=False, dry_run=False, analysis_config=None):
    if not isinstance(photo_ids, list) or any(not isinstance(i, str) or not i for i in photo_ids):
        raise PhotographyError("INVALID_ARGUMENT", "Photo IDs must be an array of non-empty strings.")
    ids = list(dict.fromkeys(photo_ids))
    profile = encoder.profile()
    if profile.get("recipe_version") != RECIPE_VERSION:
        raise PhotographyError("ENCODER_UNSUPPORTED", "Encoder uses a different text recipe.")
    encoder_id = fingerprint(profile)
    result = {"status": "completed", "requested": len(ids), "generated": 0, "cached": 0,
              "skipped": 0, "failed": 0, "pending": 0, "results": [], "encoder_id": encoder_id,
              "dry_run": dry_run, "local_model_calls": 0, "visual_model_calls": 0}
    started = time.perf_counter()
    initial_calls = getattr(encoder, "calls", 0)
    for photo_id in ids:
        try:
            photo = store.photo(photo_id)
            entry, document, _ = inspect_embedding(photo, store, profile, store.embedding(photo_id, encoder_id), analysis_config=analysis_config)
            if entry["status"] == "needs_analysis":
                result["skipped"] += 1
                result["results"].append(entry)
                continue
            if entry["status"] == "ready" and not force:
                result["cached"] += 1
                result["results"].append({**entry, "status": "cached"})
                continue
            # An invalid stored vector is repairable when a valid description exists.
            if document is None:
                document, _ = current_document(photo, store, analysis_config)
            if dry_run:
                result["pending"] += 1
                result["results"].append({**entry, "status": "pending"})
                continue
            encoding = encoder.encode(document["text"])
            blob = pack_vector(encoding.vector, profile["dimensions"])
            record = {key: document[key] for key in ("photo_id", "analysis_id", "content_version", "text_hash", "recipe_version")}
            record.update(encoder_id=encoder_id, dimensions=profile["dimensions"], dtype="float32-le", normalized=1,
                vector=blob, vector_hash=hashlib.sha256(blob).hexdigest(), token_count=encoding.token_count,
                truncated=int(encoding.truncated), created_at=now())
            with store.transaction():
                current, _ = current_document(store.photo(photo_id), store, analysis_config)
                if current != document:
                    raise PhotographyError("DESCRIPTION_CHANGED", "Description changed during encoding; rerun embed to retry.")
                store.put_embedding(record, profile)
            result["generated"] += 1
            result["results"].append({"photo_id": photo_id, "status": "generated", "analysis_id": document["analysis_id"],
                "token_count": encoding.token_count, "truncated": encoding.truncated, "encoding_seconds": encoding.elapsed_seconds})
        except PhotographyError as exc:
            result["failed"] += 1
            result["results"].append({"photo_id": photo_id, "status": "failed", "error": exc.to_dict()})
            if exc.code.startswith("EMBEDDING_MODEL_") or exc.code == "EMBEDDING_DEPENDENCY_MISSING":
                result["status"] = "blocked"
                break
        except sqlite3.Error as exc:
            result["failed"] += 1
            result["results"].append({"photo_id": photo_id, "status": "failed",
                                      "error": {"code": "STORAGE_UNAVAILABLE", "message": str(exc)}})
        except KeyboardInterrupt:
            result.update(status="partial", interrupted=True)
            break
    if result["status"] == "completed" and result["failed"]:
        result["status"] = "partial"
    result["unprocessed"] = result["requested"] - len(result["results"])
    result["elapsed_seconds"] = time.perf_counter() - started
    result["local_model_calls"] = getattr(encoder, "calls", initial_calls) - initial_calls
    result["model_load_seconds"] = getattr(encoder, "load_seconds", 0)
    return result
