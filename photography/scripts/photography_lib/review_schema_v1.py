"""Frozen v1 schema/profile and payload validation for historical records only."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
import re

from .config import PhotographyError
from .fingerprints import fingerprint


DIMENSIONS = ("composition", "lighting", "color", "subject", "storytelling", "technical")
SCHEMA_VERSION = "photo-review-v1"
RUBRIC_VERSION = "photo-review-v1"
SDK_VERSION = "1.0.13"
RUNTIME_VERSION = "1.0.83"
PROVIDER_ID = "github-copilot"
MAX_RESPONSE_BYTES = 1024 * 1024
TEXT_LIMIT = 4000
LIST_LIMIT = 8
REVIEW_FIELDS = ("description", "strengths", "improvements", "scores", "limitations")
PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "photo-review-v1.txt"

_TEXT_SCHEMA = {"type": "string", "minLength": 1, "maxLength": TEXT_LIMIT}
_LIST_SCHEMA = {"type": "array", "minItems": 1, "maxItems": LIST_LIMIT, "items": _TEXT_SCHEMA}
_SCORE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["score", "reason"],
    "properties": {"score": {"type": "number", "minimum": 0, "maximum": 10},
                   "reason": _TEXT_SCHEMA},
}
_REVIEW_PROPERTIES = {
    "image_id": {"type": "string", "pattern": "^image_[1-9][0-9]*$"},
    "description": _TEXT_SCHEMA,
    "strengths": _LIST_SCHEMA,
    "improvements": _LIST_SCHEMA,
    "limitations": _LIST_SCHEMA,
    "scores": {"type": "object", "additionalProperties": False, "required": list(DIMENSIONS),
               "properties": {key: _SCORE_SCHEMA for key in DIMENSIONS}},
}
RESPONSE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "reviews"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "reviews": {"type": "array", "minItems": 1,
                    "items": {"type": "object", "additionalProperties": False,
                              "required": ["image_id", *REVIEW_FIELDS],
                              "properties": _REVIEW_PROPERTIES}},
    },
}


def _invalid(message, *, details=None):
    raise PhotographyError("REVIEW_RESPONSE_INVALID", message, details=details)


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        _invalid("Review object has missing, unexpected, or invalid fields.")


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > TEXT_LIMIT:
        _invalid("Review text must be nonempty and within the field limit.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _invalid("Review text is not valid UTF-8.")
    return value


def _number(value):
    if (type(value) not in (int, float) or not 0 <= value <= 10
            or not math.isfinite(value)):
        _invalid("Review scores must be finite numbers from 0 to 10, not booleans.")
    return value


def _review_fields(value):
    _text(value["description"])
    for key in ("strengths", "improvements", "limitations"):
        items = value[key]
        if type(items) is not list or not 1 <= len(items) <= LIST_LIMIT:
            _invalid("Review lists must contain between one and eight text entries.")
        for item in items:
            _text(item)
    _keys(value["scores"], DIMENSIONS)
    for item in value["scores"].values():
        _keys(item, ("score", "reason"))
        _number(item["score"])
        _text(item["reason"])


def overall_score(scores):
    values = [Decimal(str(_number(scores[key]["score"]))) for key in DIMENSIONS]
    return float((sum(values) / Decimal(len(values))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def validate_payload(value):
    """Validate the canonical saved payload, including its application-computed mean."""
    _keys(value, ("schema_version", "overall_score", *REVIEW_FIELDS))
    if value["schema_version"] != SCHEMA_VERSION:
        _invalid("Unsupported review payload schema.")
    _review_fields(value)
    if _number(value["overall_score"]) != overall_score(value["scores"]):
        _invalid("Saved overall score does not match the six dimension scores.")
    return deepcopy(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _nonfinite(_value):
    raise ValueError("Non-finite JSON number.")


def review_profile(model, language="zh-CN"):
    if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model)
            or model.casefold() in ("auto", "default")):
        raise PhotographyError("INVALID_ARGUMENT", "Choose an explicit Copilot vision model ID, not auto.")
    if language not in ("zh-CN", "en"):
        raise PhotographyError("INVALID_ARGUMENT", "Review language must be zh-CN or en.")
    try:
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PhotographyError("REVIEW_RUBRIC_UNAVAILABLE", "The bundled review rubric cannot be read.") from exc
    profile = {
        "version": "ai-review-profile-v1", "provider": PROVIDER_ID, "model": model,
        "sdk_version": SDK_VERSION, "runtime_version": RUNTIME_VERSION,
        "rubric_version": RUBRIC_VERSION, "output_schema_version": SCHEMA_VERSION,
        "language": language, "input_scope": "stored_thumbnail",
        "auth_policy": "current-copilot-login", "dimensions": list(DIMENSIONS),
        "weights": {key: 1 for key in DIMENSIONS}, "rounding": "decimal-half-up-2",
        "prompt_text": prompt, "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "response_schema_hash": fingerprint(RESPONSE_SCHEMA),
    }
    return validate_profile(profile)


def validate_profile(profile):
    expected = {
        "version", "provider", "model", "sdk_version", "runtime_version", "rubric_version",
        "output_schema_version", "language", "input_scope", "auth_policy", "dimensions",
        "weights", "rounding", "prompt_text", "prompt_hash", "response_schema_hash",
    }
    valid = (type(profile) is dict and set(profile) == expected)
    if valid:
        valid = (
            profile["version"] == "ai-review-profile-v1" and profile["provider"] == PROVIDER_ID
            and isinstance(profile["model"], str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", profile["model"])
            and profile["model"].casefold() not in ("auto", "default")
            and profile["sdk_version"] == SDK_VERSION and profile["runtime_version"] == RUNTIME_VERSION
            and profile["rubric_version"] == RUBRIC_VERSION and profile["output_schema_version"] == SCHEMA_VERSION
            and profile["language"] in ("zh-CN", "en") and profile["input_scope"] == "stored_thumbnail"
            and profile["auth_policy"] == "current-copilot-login"
            and profile["dimensions"] == list(DIMENSIONS)
            and type(profile["weights"]) is dict and set(profile["weights"]) == set(DIMENSIONS)
            and all(type(value) is int and value == 1 for value in profile["weights"].values())
            and profile["rounding"] == "decimal-half-up-2"
            and isinstance(profile["prompt_text"], str) and 1 <= len(profile["prompt_text"]) <= 16000
            and profile["response_schema_hash"] == fingerprint(RESPONSE_SCHEMA)
        )
    if valid:
        try:
            valid = profile["prompt_hash"] == hashlib.sha256(profile["prompt_text"].encode("utf-8")).hexdigest()
        except UnicodeError:
            valid = False
    if not valid:
        raise PhotographyError("REVIEW_PROFILE_INVALID", "Review configuration failed its version or integrity check.")
    return deepcopy(profile)
