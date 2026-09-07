"""Feature input identities from saved data, with explicit original-file verification."""
from __future__ import annotations

import hashlib
import io
import os
import stat
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import PhotographyError
from .fingerprints import fingerprint
from .images import signature
from .source_paths import resolve_original
from .thumbnails import stored_preview


MAX_ORIGINAL_BYTES = 256 * 1024 * 1024


def _photo(store, photo_id):
    photo = store.photo(photo_id)
    if photo["ingest_state"] != "available":
        raise PhotographyError("FEATURE_INVALID_INPUT", "Repair or reingest this photo before indexing.",
                               details={"photo_id": photo_id})
    return photo


def manifest_for(photo_id, profile, *, store):
    """Inspect saved identities only; status and current-result lookup never probe originals."""
    photo = _photo(store, photo_id)
    scope = profile["input_scope"]
    manifest = {"content_version": photo["content_version"], "input_scope": scope}
    if scope == "stored_thumbnail":
        from .image_embedding import _preview_input

        _preview_input(photo, store, check_preview=False)
        thumbnail = store.thumbnail(photo_id, include_data=False)
        manifest.update(thumbnail_profile=thumbnail["profile"], input_image_hash=thumbnail["image_hash"],
                        width=thumbnail["width"], height=thumbnail["height"])
    elif scope == "original":
        metadata = photo["metadata"]
        width, height = metadata.get("display_width"), metadata.get("display_height")
        if any(type(value) is not int or value < 1 for value in (width, height)):
            raise PhotographyError("FEATURE_INVALID_INPUT", "Saved original display dimensions are missing; reingest.")
        if photo["size_bytes"] > MAX_ORIGINAL_BYTES or width * height > profile["parameters"]["max_input_pixels"]:
            raise PhotographyError("FEATURE_INVALID_INPUT", "Saved original dimensions or bytes exceed the OCR input limit.")
        manifest.update(original_hash=photo["content_version"], width=width, height=height)
    elif scope == "object_result":
        source_profile = store.feature_profile(profile["dependencies"]["objects"])
        if source_profile["component"] != "objects":
            raise PhotographyError("FEATURE_PROFILE_INVALID", "Composition requires an objects profile.")
        source_manifest = manifest_for(photo_id, source_profile, store=store)
        source = store.find_feature_result(photo_id, profile["dependencies"]["objects"], fingerprint(source_manifest))
        if source is None:
            raise PhotographyError("FEATURE_DEPENDENCY_MISSING", "Build the explicitly selected objects index first.")
        manifest.update(source_result_id=source["result_id"], source_profile_id=source["profile_id"],
                        source_payload_hash=source["payload_hash"],
                        width=source["payload"]["width"], height=source["payload"]["height"])
    elif scope == "image_embedding":
        from .image_embedding import inspect_embedding

        embedding_id = profile["dependencies"]["image_embedding"]
        embedding_profile = store.embedding_profile(embedding_id)
        entry, record, _ = inspect_embedding(photo, store, embedding_profile)
        if entry["status"] != "ready":
            raise PhotographyError("FEATURE_DEPENDENCY_MISSING", "Build the explicitly selected image embedding first.",
                                   details={"photo_id": photo_id, "embedding_status": entry["status"]})
        prototypes = store.scene_prototype_set(fingerprint(profile))
        if prototypes is None:
            raise PhotographyError("FEATURE_DEPENDENCY_MISSING", "Prepare this scene profile's text prototypes first.")
        manifest.update(embedding_result_id=record["result_id"], embedding_profile_id=embedding_id,
                        vector_hash=record["vector_hash"], prototype_set_id=prototypes["set_id"],
                        prototype_hash=prototypes["payload_hash"])
    else:
        raise PhotographyError("FEATURE_PROFILE_INVALID", "Unknown feature input scope.")
    return manifest


def verify_manifest(photo_id, profile, expected, *, store):
    current = manifest_for(photo_id, profile, store=store)
    if fingerprint(current) != fingerprint(expected):
        raise PhotographyError("FEATURE_INPUT_CHANGED", "Saved feature input or dependency changed; prepare a new plan.")
    return current


def _original_bytes(photo, manifest, database_path):
    resolution = resolve_original(photo, database_path)
    if resolution["status"] != "available":
        raise PhotographyError("FEATURE_INPUT_UNAVAILABLE", "The original is not available for OCR.",
                               details={"photo_id": photo["photo_id"], "resolution": resolution})
    from pathlib import Path

    path = Path(resolution["path"])
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != photo["size_bytes"]:
                raise PhotographyError("FEATURE_INPUT_CHANGED", "Original file size or type changed after ingestion.")
            if before.st_size > MAX_ORIGINAL_BYTES:
                raise PhotographyError("FEATURE_INVALID_INPUT", "OCR original exceeds the 256 MiB input limit.")
            data = source.read(MAX_ORIGINAL_BYTES + 1)
            if len(data) > MAX_ORIGINAL_BYTES:
                raise PhotographyError("FEATURE_INVALID_INPUT", "OCR input grew beyond the 256 MiB input limit.")
            after = os.fstat(source.fileno())
            path_after = path.stat()
        if signature(before) != signature(after) or signature(before) != signature(path_after):
            raise PhotographyError("FEATURE_INPUT_CHANGED", "Original changed while reading.")
        if hashlib.sha256(data).hexdigest() != manifest["original_hash"]:
            raise PhotographyError("FEATURE_INPUT_CHANGED", "Original bytes differ from the ingested version; reingest.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if getattr(image, "n_frames", 1) != 1:
                    raise PhotographyError("FEATURE_INVALID_INPUT", "OCR expects a single-frame photo.")
                image.load()
                with ImageOps.exif_transpose(image) as oriented:
                    if oriented.size != (manifest["width"], manifest["height"]):
                        raise PhotographyError("FEATURE_INPUT_CHANGED", "Original dimensions differ from ingestion metadata.")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, ValueError, SyntaxError) as exc:
        raise PhotographyError("FEATURE_INPUT_UNAVAILABLE", "Cannot read or decode the original: " + str(exc)) from exc
    return data


def load_input(photo_id, profile, manifest, *, store):
    """Load/verify input outside write transactions; returns pixels only to local providers."""
    if store.db.in_transaction:
        raise PhotographyError("FEATURE_TRANSACTION_ACTIVE", "Load feature inputs outside SQLite transactions.")
    with store.read_snapshot():
        verify_manifest(photo_id, profile, manifest, store=store)
        photo = _photo(store, photo_id)
        scope = profile["input_scope"]
        if scope == "stored_thumbnail":
            data = stored_preview(photo, store)
            if hashlib.sha256(data).hexdigest() != manifest["input_image_hash"]:
                raise PhotographyError("FEATURE_INPUT_CHANGED", "Preview bytes differ from the frozen input.")
            return data
        if scope == "object_result":
            return store.feature_result(manifest["source_result_id"])["payload"]
        if scope == "image_embedding":
            from .image_embedding import inspect_embedding

            _, record, vector = inspect_embedding(photo, store, store.embedding_profile(manifest["embedding_profile_id"]))
            if record is None or record["result_id"] != manifest["embedding_result_id"]:
                raise PhotographyError("FEATURE_INPUT_CHANGED", "Source embedding is no longer available.")
            return (vector, store.scene_prototype_set(fingerprint(profile))["prototypes"])
    return _original_bytes(photo, manifest, store.database_path)
