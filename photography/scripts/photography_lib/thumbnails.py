"""Validation of stored previews, independent of original-file availability."""
from __future__ import annotations

import hashlib
import io
from PIL import Image, UnidentifiedImageError
from .config import PhotographyError

MAX_PREVIEW_BYTES = 20 * 1024 * 1024


def validate_preview(data: bytes) -> tuple[int, int]:
    try:
        if not data or len(data) > MAX_PREVIEW_BYTES:
            raise ValueError("Invalid preview size")
        with Image.open(io.BytesIO(data)) as preview:
            if preview.format != "JPEG" or max(preview.size) > 4096:
                raise ValueError("Expected a JPEG preview up to 4096 pixels")
            preview.load()
            return preview.size
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise PhotographyError("INVALID_PREVIEW", "Stored preview is missing or invalid; repair it by rescanning.") from exc


def stored_preview(photo, store) -> bytes:
    item = store.thumbnail(photo["photo_id"])
    if (item["content_version"], item["profile"]) != (photo["content_version"], photo["thumbnail_profile"]):
        raise PhotographyError("INVALID_PREVIEW", "Stored preview does not match the indexed photo version.")
    data = item["data"]
    if hashlib.sha256(data).hexdigest() != item["image_hash"]:
        raise PhotographyError("INVALID_PREVIEW", "Stored preview failed its content check; rescan to repair it.")
    if validate_preview(data) != (item["width"], item["height"]):
        raise PhotographyError("INVALID_PREVIEW", "Stored preview dimensions do not match its metadata.")
    return data
