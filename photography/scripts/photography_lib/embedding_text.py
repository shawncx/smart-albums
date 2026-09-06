"""Versioned, deterministic retrieval text; no provider or filesystem access."""
from __future__ import annotations

import hashlib
import unicodedata

from .config import PhotographyError

RECIPE_VERSION = "photo-text-v1"
FIELDS = (("visual_description", "描述"), ("subjects", "主体"), ("scene", "场景"),
          ("composition", "构图"), ("color", "色彩"), ("lighting", "光线"),
          ("mood", "氛围"), ("tags", "标签"))


def retrieval_text(data):
    if not isinstance(data, dict) or not isinstance(data.get("visual_description"), str) or not data["visual_description"].strip():
        raise PhotographyError("INVALID_DESCRIPTION", "A non-empty saved visual description is required.")
    parts = []
    for key, label in FIELDS:
        value = data.get(key, "")
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            value = "；".join(value)
        if not isinstance(value, str):
            raise PhotographyError("INVALID_DESCRIPTION", f"Invalid saved text field: {key}.")
        value = " ".join(unicodedata.normalize("NFC", value).split())
        if value:
            parts.append(label + "：" + value)
    text = "\n".join(parts)
    # The model applies a separate, recorded 512-token truncation policy.
    if len(text) > 100_000:
        raise PhotographyError("INVALID_DESCRIPTION", "Saved description is too large for this text recipe.")
    return text


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
