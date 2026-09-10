"""Strict v2 provider contract, with separate validation of saved v1 history."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .config import PhotographyError
from .fingerprints import fingerprint
from . import review_schema_v1 as legacy

DIMENSIONS = legacy.DIMENSIONS
SCHEMA_VERSION = RUBRIC_VERSION = "photo-review-v2"
SDK_VERSION, RUNTIME_VERSION, PROVIDER_ID = legacy.SDK_VERSION, legacy.RUNTIME_VERSION, legacy.PROVIDER_ID
MAX_RESPONSE_BYTES, TEXT_LIMIT, LIST_LIMIT = legacy.MAX_RESPONSE_BYTES, legacy.TEXT_LIMIT, legacy.LIST_LIMIT
REVIEW_FIELDS = ("review_status", "description", "dimensions", "strengths", "improvements", "limitations")
PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "photo-review-v2.txt"
_JSON_BLOCK = re.compile(r"```(?P<tag>json)?[ \t]*\r?\n(?P<body>.*?)\r?\n```", re.IGNORECASE | re.DOTALL)
_JSON_ERROR_CODES = {
    "Expecting value": "expected_value",
    "Expecting property name enclosed in double quotes": "expected_property_name",
    "Expecting ':' delimiter": "expected_colon",
    "Expecting ',' delimiter": "expected_comma",
    "Extra data": "extra_data",
    "Unterminated string starting at": "unterminated_string",
    "Invalid control character at": "invalid_control_character",
    "Invalid \\escape": "invalid_escape",
    "Invalid \\uXXXX escape": "invalid_unicode_escape",
    "Illegal trailing comma before end of object": "trailing_object_comma",
    "Illegal trailing comma before end of array": "trailing_array_comma",
}

_TEXT_SCHEMA = {"type": "string", "minLength": 1, "maxLength": TEXT_LIMIT, "pattern": r"\S"}
_NUMBER_SCHEMA = {"type": "number", "minimum": 0, "maximum": 10, "multipleOf": 0.5}
_SCORE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["score", "reason"],
    "properties": {"score": {"anyOf": [_NUMBER_SCHEMA, {"type": "null"}]}, "reason": _TEXT_SCHEMA},
}
_IMPROVEMENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "action", "rationale", "tradeoff"],
    "properties": {"kind": {"enum": ["edit", "reshoot"]}, "action": _TEXT_SCHEMA,
                   "rationale": _TEXT_SCHEMA, "tradeoff": {"anyOf": [_TEXT_SCHEMA, {"type": "null"}]}},
}


def _score_types(kind):
    return {key: {"properties": {"score": {"type": kind}}} for key in DIMENSIONS}


_REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["image_id", *REVIEW_FIELDS],
    "properties": {
        "image_id": {"type": "string"},
        "review_status": {"enum": ["reviewed", "partial", "unreviewable"]},
        "description": _TEXT_SCHEMA,
        "dimensions": {"type": "object", "additionalProperties": False, "required": list(DIMENSIONS),
                       "properties": {key: _SCORE_SCHEMA for key in DIMENSIONS}},
        "strengths": {"type": "array", "maxItems": 3, "items": _TEXT_SCHEMA},
        "improvements": {"type": "array", "maxItems": 3, "items": _IMPROVEMENT_SCHEMA},
        "limitations": {"type": "array", "minItems": 1, "items": _TEXT_SCHEMA},
    },
    "allOf": [
        {"if": {"properties": {"review_status": {"const": "reviewed"}}},
         "then": {"properties": {"dimensions": {"properties": _score_types("number")}}}},
        {"if": {"properties": {"review_status": {"const": "unreviewable"}}},
         "then": {"properties": {"dimensions": {"properties": _score_types("null")}}}},
        {"if": {"properties": {"review_status": {"const": "partial"}}},
         "then": {"properties": {"dimensions": {"allOf": [
             {"anyOf": [{"properties": {key: value}} for key, value in _score_types(kind).items()]}
             for kind in ("number", "null")
         ]}}}},
    ],
}
RESPONSE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False, "required": ["results"],
    "properties": {"results": {"type": "array", "items": _REVIEW_SCHEMA}},
}
_invalid, _keys, _text, _number = legacy._invalid, legacy._keys, legacy._text, legacy._number


def _review_fields(value):
    _text(value["description"])
    for key in ("strengths", "limitations"):
        entries = value[key]
        if type(entries) is not list or (key == "strengths" and len(entries) > 3) or (
                key == "limitations" and not entries):
            _invalid("Strengths require 0–3 entries; limitations must be nonempty.")
        for entry in entries:
            _text(entry)
    entries = value["improvements"]
    if type(entries) is not list or len(entries) > 3:
        _invalid("Improvements must contain 0–3 structured suggestions.")
    for entry in entries:
        _keys(entry, ("kind", "action", "rationale", "tradeoff"))
        if entry["kind"] not in ("edit", "reshoot"):
            _invalid("Improvement kind must be edit or reshoot.")
        _text(entry["action"])
        _text(entry["rationale"])
        if entry["tradeoff"] is not None:
            _text(entry["tradeoff"])
    _keys(value["dimensions"], DIMENSIONS)
    numeric = 0
    for entry in value["dimensions"].values():
        _keys(entry, ("score", "reason"))
        _text(entry["reason"])
        if entry["score"] is not None:
            score = _number(entry["score"])
            if score * 2 != int(score * 2):
                _invalid("Review scores must use increments of 0.5.")
            numeric += 1
    status = "reviewed" if numeric == 6 else "partial" if numeric else "unreviewable"
    if value["review_status"] != status:
        _invalid("Review status does not match the six numeric/null dimension scores.")


def dimension_scores(payload):
    return payload["scores" if payload["schema_version"] == legacy.SCHEMA_VERSION else "dimensions"]


def overall_score(scores):
    """Application-only mean; incomplete evidence never becomes a numeric total."""
    if any(scores[key]["score"] is None for key in DIMENSIONS):
        return None
    return legacy.overall_score(scores)


def validate_payload(value):
    if type(value) is dict and value.get("schema_version") == legacy.SCHEMA_VERSION:
        return legacy.validate_payload(value)
    _keys(value, ("schema_version", "overall_score", *REVIEW_FIELDS))
    if value["schema_version"] != SCHEMA_VERSION:
        _invalid("Unsupported review payload schema.")
    _review_fields(value)
    expected = overall_score(value["dimensions"])
    actual = value["overall_score"]
    if (expected is None and actual is not None) or (
            expected is not None and _number(actual) != expected):
        _invalid("Saved overall score does not match the complete six-dimension mean.")
    return deepcopy(value)


def _response_body(text):
    """Remove at most one complete transport wrapper, never extract or repair JSON."""
    stripped = text.strip(" \t\r\n")
    block = _JSON_BLOCK.fullmatch(stripped)
    if block is not None:
        return block["body"], "json_code_block" if block["tag"] else "unlabeled_code_block"
    kind = ("empty" if not stripped else "object" if stripped.startswith("{") else
            "array" if stripped.startswith("[") else "code_fence" if stripped.startswith("```") else "other")
    return text, kind


def response_diagnostics(text):
    """Describe transport shape without retaining model text or image content."""
    if not isinstance(text, str):
        return {"response_format": "non_text"}
    try:
        data = text.encode("utf-8")
    except UnicodeError:
        return {"response_format": "invalid_utf8"}
    return {
        "response_format": _response_body(text)[1] if len(data) <= MAX_RESPONSE_BYTES else "oversized",
        "response_bytes": len(data), "response_chars": len(text),
        "response_sha256": hashlib.sha256(data).hexdigest(),
    }


def _json_error_diagnostics(error):
    body, offset = error.doc, error.pos
    character = body[offset:offset + 1]
    kinds = {'"': "quote", "\\": "backslash", "{": "object_start", "}": "object_end",
             "[": "array_start", "]": "array_end", ",": "comma", ":": "colon"}
    character_kind = ("end_of_input" if not character else
                      kinds.get(character, "whitespace" if character.isspace() else "other"))
    return {
        "json_error_code": _JSON_ERROR_CODES.get(error.msg, "invalid_json"),
        "json_error_offset": offset,
        "json_error_character": character_kind,
        "json_error_at_end": not body[offset:].strip(),
        "json_body_chars": len(body),
        "json_remaining_chars": len(body) - offset,
        "line": error.lineno, "column": error.colno,
    }


def strict_json(text):
    diagnostics = response_diagnostics(text)
    try:
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
            _invalid("Review response is missing or exceeds the size limit.", details=diagnostics)
        body, _ = _response_body(text)
        return json.loads(body, object_pairs_hook=legacy._unique_object, parse_constant=legacy._nonfinite)
    except json.JSONDecodeError as exc:
        _invalid("Expected one JSON object, optionally inside a single JSON or unlabeled code block; "
                 "surrounding prose and malformed JSON are not accepted.",
                 details={**diagnostics, **_json_error_diagnostics(exc)})
    except (ValueError, UnicodeError, RecursionError):
        _invalid("Expected one strict JSON object without duplicate keys or non-finite numbers.",
                 details=diagnostics)


def _image_ids(ids):
    # IDs are opaque strings. Their contents are never filenames or evidence.
    if (not isinstance(ids, (list, tuple)) or any(not isinstance(key, str) for key in ids)
            or len(set(ids)) != len(ids)):
        raise PhotographyError("INVALID_ARGUMENT", "Expected distinct opaque review image IDs.")
    try:
        json.dumps(ids, ensure_ascii=False).encode("utf-8")
    except UnicodeError:
        raise PhotographyError("INVALID_ARGUMENT", "Review image IDs must be valid UTF-8.") from None


def parse_response(text, expected_ids):
    _image_ids(expected_ids)
    try:
        return _parse_response(text, expected_ids)
    except PhotographyError as exc:
        if exc.code == "REVIEW_RESPONSE_INVALID":
            exc.details = {**response_diagnostics(text), **(exc.details or {})}
        raise


def _parse_response(text, expected_ids):
    envelope = strict_json(text)
    _keys(envelope, ("results",))
    reviews = envelope["results"]
    if type(reviews) is not list or len(reviews) != len(expected_ids):
        _invalid("The response must contain exactly one result per manifest entry.")
    results = {}
    for review, key in zip(reviews, expected_ids):
        _keys(review, ("image_id", *REVIEW_FIELDS))
        if review["image_id"] != key:
            _invalid("Result image IDs must match the manifest exactly, in manifest order.")
        _review_fields(review)
        payload = {name: deepcopy(review[name]) for name in REVIEW_FIELDS}
        payload.update(schema_version=SCHEMA_VERSION, overall_score=overall_score(payload["dimensions"]))
        results[key] = validate_payload(payload)
    return results


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
    if type(profile) is dict and profile.get("output_schema_version") == legacy.SCHEMA_VERSION:
        return legacy.validate_profile(profile)
    if (type(profile) is not dict or profile.get("rubric_version") != RUBRIC_VERSION
            or profile.get("output_schema_version") != SCHEMA_VERSION
            or profile.get("response_schema_hash") != fingerprint(RESPONSE_SCHEMA)):
        raise PhotographyError("REVIEW_PROFILE_INVALID", "Review configuration failed its version or integrity check.")
    # Transport/auth/profile shape is unchanged; substitute only versioned
    # identities to reuse the frozen integrity rules without mutating history.
    probe = {**profile, "rubric_version": legacy.RUBRIC_VERSION,
             "output_schema_version": legacy.SCHEMA_VERSION,
             "response_schema_hash": fingerprint(legacy.RESPONSE_SCHEMA)}
    legacy.validate_profile(probe)
    return deepcopy(profile)


def build_prompt(profile, image_ids):
    profile = validate_profile(profile)
    if profile["output_schema_version"] != SCHEMA_VERSION:
        raise PhotographyError("REVIEW_PROFILE_CHANGED", "Create a new plan using the current review rubric.")
    _image_ids(image_ids)
    schema = deepcopy(RESPONSE_SCHEMA)
    results = schema["properties"]["results"]
    schema["$defs"] = {"review": results.pop("items")}
    results["items"] = {"$ref": "#/$defs/review"}
    results.update(minItems=len(image_ids), maxItems=len(image_ids))
    # Bind each position as well as membership; reordered results are invalid.
    if image_ids:
        results["prefixItems"] = [
            {"allOf": [{"$ref": "#/$defs/review"}, {"properties": {"image_id": {"const": key}}}]}
            for key in image_ids
        ]
    language = "Simplified Chinese" if profile["language"] == "zh-CN" else "English"
    manifest = [{"image_id": key, "attachment_index": i + 1} for i, key in enumerate(image_ids)]
    return (profile["prompt_text"] + "\nRequest input:\n" + json.dumps({
        "output_language": language, "manifest": manifest,
    }, ensure_ascii=False) + "\nReturn only JSON conforming to this schema:\n"
        + json.dumps(schema, ensure_ascii=False, sort_keys=True))
