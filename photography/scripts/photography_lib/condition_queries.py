"""Strict, portable queries and pixel-free per-condition evidence."""
from __future__ import annotations

from copy import deepcopy
import json
import math
import unicodedata

from .config import PhotographyError
from .feature_profiles import normalize_ocr_text
from .virtual_folders import _ids as folder_ids


SCHEMA = "multi-condition-query-v1"
COMPONENTS = {
    "object_count": "objects", "ocr_contains": "ocr", "color_fraction": "color",
    "subject_position": "composition", "scene": "scene",
    "has_near_duplicate": "perceptual_hash",
}
KINDS = ("semantic", *COMPONENTS)
STATUSES = (
    "matched", "not_matched", "unknown_missing_index", "unknown_stale",
    "unknown_invalid", "unsupported", "not_reviewed",
)
COLORS = ("black", "white", "gray", "red", "orange", "yellow", "green",
          "cyan", "blue", "purple", "magenta")
PALETTE_RULE_VERSION = "hsv-palette-v1"
POSITION_RULE_VERSION = "thirds-v1"
_FIELDS = {
    "semantic": {"query", "visual_query"},
    "object_count": {"class_id", "operator", "value", "score_threshold"},
    "ocr_contains": {"text"},
    "color_fraction": {"color", "minimum"},
    "subject_position": {"horizontal", "vertical"},
    "scene": {"scene_id", "minimum"},
    "has_near_duplicate": {"metric", "max_distance"},
}
_SOURCE_FIELDS = {
    "feature": {"kind", "photo_id", "profile_id", "result_id", "input_fingerprint", "payload_hash"},
    "embedding": {"kind", "photo_id", "profile_id", "result_id", "content_version",
                  "thumbnail_profile", "input_image_hash", "vector_hash"},
    "photo": {"kind", "photo_id", "content_version", "ingest_state"},
}
_EVIDENCE_FIELDS = {
    "semantic": {"candidate_rank", "score_gap_from_best", "score_gap_to_next"},
    "object_count": {"count"},
    "ocr_contains": {"snippet"},
    "color_fraction": {"fraction", "rule_version"},
    "subject_position": {"center_x", "center_y", "rule_version"},
    "scene": {"score"},
    "has_near_duplicate": {"best_distance", "peer_id", "peer_count"},
}


def _require(valid, message):
    if not valid:
        raise PhotographyError("INVALID_ARGUMENT", message)


def _fields(value, allowed, required=()):
    _require(isinstance(value, dict), "Expected a query or evidence object.")
    _require(set(required) <= set(value) <= set(allowed), "Missing or unknown query/evidence fields.")


def _text(value, name, *, limit=4096):
    _require(isinstance(value, str) and 0 < len(value) <= limit and bool(value.strip()),
             f"{name} must be nonblank text of at most {limit} characters.")
    _require(not any(unicodedata.category(char) == "Cs" or char == "\x00" for char in value),
             f"{name} contains invalid text.")
    return value


def _id(value, name="ID"):
    _text(value, name, limit=256)
    _require(not any(char.isspace() or char in "\\/" or unicodedata.category(char) in ("Cc", "Cf")
                     for char in value), f"{name} must not contain paths, whitespace or control characters.")
    return value


def _number(value, low, high, name):
    _require(type(value) in (int, float), f"{name} must be a finite number.")
    try:
        valid = math.isfinite(value) and low <= value <= high
    except OverflowError:
        valid = False
    _require(valid, f"{name} must be finite and between {low} and {high}.")
    return float(value)


def _integer(value, low, high, name):
    _require(type(value) is int and low <= value <= high,
             f"{name} must be an integer between {low} and {high}.")
    return value


def _choice(value, choices, name):
    _require(isinstance(value, str) and value in choices, f"Unsupported {name}.")
    return value


def _scope(raw):
    _fields(raw, {"folder_ids", "match"})
    ids = sorted(folder_ids(raw.get("folder_ids", []), "Folder"))
    for value in ids:
        _id(value, "Folder ID")
    match = raw.get("match")
    if not ids:
        _require(match is None, "Folder match requires folder IDs.")
    else:
        _require(match is not None or len(ids) == 1,
                 "Multiple folders require an explicit union or intersection.")
        match = _choice("union" if match is None else match, ("union", "intersection"), "folder match")
    return {"folder_ids": ids, "match": match}


def _profile(condition, store, cache):
    kind, selected = condition["kind"], condition.get("profile_id")
    if kind == "has_near_duplicate" and condition["metric"] == "exact":
        _require(selected is None, "Exact duplicates do not use a feature profile.")
        return None, None
    component = "semantic" if kind == "semantic" else COMPONENTS[kind]
    if selected is not None:
        _id(selected, "Profile ID")
    key = (component, selected)
    if key not in cache:
        if kind == "semantic":
            from .image_embedding import _dimensions, resolve_profile
            from .image_embedding_storage import profile_identity

            profile = resolve_profile(store, selected)
            _dimensions(profile)
        else:
            from .feature_index import resolve_profile
            from .feature_profiles import profile_identity

            profile = resolve_profile(component, store=store, profile_id=selected)
            if component == "composition":
                source = store.feature_profile(profile["dependencies"]["objects"])
                _require(source["component"] == "objects", "Composition requires an objects profile.")
                subject = profile["parameters"]["subject_class"]
                _require(subject is None or subject in source["parameters"]["labels"],
                         "The composition profile selects an unsupported object class.")
            elif component == "scene":
                from .image_embedding import _dimensions

                _dimensions(store.embedding_profile(profile["dependencies"]["image_embedding"]))
        cache[key] = (profile_identity(profile)[0], profile)
    return cache[key]


def _condition(raw, store, cache):
    _fields(raw, {"id", "kind", "profile_id", "scoring"} | set().union(*_FIELDS.values()), ("id", "kind"))
    kind = _choice(raw["kind"], KINDS, "condition kind")
    _fields(raw, {"id", "kind", "profile_id", "scoring"} | _FIELDS[kind], ("id", "kind"))
    result = {"id": _id(raw["id"], "Condition ID"), "kind": kind}
    scoring = raw.get("scoring", "graded" if kind in ("semantic", "color_fraction", "scene") else "exact")
    _choice(scoring, ("exact", "graded") if kind in ("color_fraction", "scene") else
            ("graded",) if kind == "semantic" else ("exact",), "condition scoring")
    if kind == "semantic":
        result["query"] = _text(raw.get("query"), "Semantic query").strip()
        if "visual_query" in raw:
            from .semantic_query import prepare_query

            visual = _text(raw["visual_query"], "English visual query")
            result["visual_query"] = prepare_query(result["query"], visual)["visual_query"]
    elif kind == "object_count":
        result.update(class_id=_text(raw.get("class_id"), "Object class", limit=128),
                      operator=_choice(raw.get("operator"), ("eq", "ge", "le"), "count operator"),
                      value=_integer(raw.get("value"), 0, 2147483647, "Object count"))
    elif kind == "ocr_contains":
        text = normalize_ocr_text(_text(raw.get("text"), "OCR literal"))
        result["text"] = _text(text, "Normalized OCR literal")
    elif kind == "color_fraction":
        result.update(color=_choice(raw.get("color"), COLORS, "palette color"),
                      minimum=_number(raw.get("minimum"), 0, 1, "Color minimum"))
    elif kind == "subject_position":
        horizontal, vertical = raw.get("horizontal"), raw.get("vertical")
        _require(horizontal is not None or vertical is not None, "Specify at least one subject-position axis.")
        if horizontal is not None:
            _choice(horizontal, ("left", "center", "right"), "horizontal position")
        if vertical is not None:
            _choice(vertical, ("top", "middle", "bottom"), "vertical position")
        result.update(horizontal=horizontal, vertical=vertical)
    elif kind == "scene":
        result.update(scene_id=_text(raw.get("scene_id"), "Scene ID", limit=256),
                      minimum=_number(raw.get("minimum"), -1, 1, "Scene minimum"))
    else:
        metric = _choice(raw.get("metric"), ("exact", "hamming"), "duplicate metric")
        distance = _integer(raw.get("max_distance", 0 if metric == "exact" else None),
                            0, 0 if metric == "exact" else 64, "Duplicate distance")
        result.update(metric=metric, max_distance=distance)
    profile_id, profile = _profile({**raw, **result}, store, cache)
    if kind == "object_count":
        _require(result["class_id"] in profile["parameters"]["labels"], "Object class is not supported by this profile.")
        floor = profile["parameters"]["score_threshold"]
        result["score_threshold"] = _number(raw.get("score_threshold", floor), floor, 1, "Object score threshold")
    elif kind == "scene":
        _require(result["scene_id"] in {item["scene_id"] for item in profile["parameters"]["catalog"]},
                 "Scene ID is not in this profile's saved catalog.")
    result.update(profile_id=profile_id, scoring=scoring)
    return result


def normalize_query(raw, *, store):
    """Resolve defaults once; folder existence and scope membership are the caller's responsibility."""
    _fields(raw, {"schema", "operator", "scope", "semantic_candidates", "review_page_size",
                  "random_seed", "conditions"}, ("conditions",))
    _require(raw.get("schema", SCHEMA) == SCHEMA, "Unsupported condition-query schema.")
    _require(raw.get("operator", "or") == "or", "Condition lists support OR only.")
    conditions = raw["conditions"]
    _require(isinstance(conditions, list) and bool(conditions), "Provide a nonempty array of conditions.")
    candidates = raw.get("semantic_candidates", 10)
    if candidates != "all":
        _integer(candidates, 1, 2147483647, "Semantic candidate count")
    page_size = _integer(raw.get("review_page_size", 100), 1, 1000, "Review page size")
    seed = raw.get("random_seed")
    if seed is not None:
        _text(seed, "Random seed", limit=256)
    canonical, aliases, identities, seen, cache = [], {}, {}, set(), {}
    for item in conditions:
        _fields(item, {"id", "kind", "profile_id", "scoring"} | set().union(*_FIELDS.values()), ("id", "kind"))
        cid = _id(item["id"], "Condition ID")
        _require(cid not in seen, "Condition IDs must be unique, even for identical predicates.")
        seen.add(cid)
        condition = _condition(item, store, cache)
        identity_fields = {key: value for key, value in condition.items() if key not in ("id", "scoring")}
        if condition["kind"] == "semantic":
            identity_fields["query"] = unicodedata.normalize("NFC", condition.get("visual_query", condition["query"]))
            identity_fields.pop("visual_query", None)
        identity = json.dumps(identity_fields,
                              sort_keys=True, ensure_ascii=False, allow_nan=False)
        if identity not in identities:
            canonical.append(condition)
            identities[identity] = condition
        first = identities[identity]
        _require(first["scoring"] == condition["scoring"],
                 "Identical predicates must use the same scoring policy.")
        aliases[cid] = first["id"]
    return {"query": {"schema": SCHEMA, "operator": "or", "scope": _scope(raw.get("scope", {})),
                      "semantic_candidates": candidates, "review_page_size": page_size,
                      "random_seed": seed, "conditions": canonical}, "aliases": aliases}


def descriptor(condition):
    """Boolean predicates do not acquire graded confidence or distance weighting."""
    _require(isinstance(condition, dict), "Expected a condition.")
    kind = _choice(condition.get("kind"), KINDS, "condition kind")
    scoring = condition.get("scoring", "graded" if kind in ("semantic", "color_fraction", "scene") else "exact")
    _choice(scoring, ("exact", "graded") if kind in ("color_fraction", "scene") else
            ("graded",) if kind == "semantic" else ("exact",), "condition scoring")
    return {"score_type": "exact" if scoring == "exact" else
            "fraction" if kind == "color_fraction" else "cosine", "direction": "higher"}


def _source(source):
    _require(isinstance(source, dict), "Source references must be objects.")
    kind = _choice(source.get("kind"), _SOURCE_FIELDS, "source kind")
    _fields(source, _SOURCE_FIELDS[kind], _SOURCE_FIELDS[kind])
    for field, value in source.items():
        if field == "ingest_state":
            _choice(value, ("available", "error"), "source ingestion state")
        elif field == "thumbnail_profile":
            _text(value, "Source thumbnail profile", limit=256)
        elif field != "kind":
            _id(value, "Source " + field)


def _evidence(condition, evidence):
    kind = condition["kind"]
    _fields(evidence, _EVIDENCE_FIELDS[kind])
    for field, value in evidence.items():
        if field == "snippet":
            _require(isinstance(value, str) and len(value) <= 160 and
                     not any(char == "\x00" or unicodedata.category(char) == "Cs" for char in value),
                     "OCR snippets must be text of at most 160 characters.")
        elif field == "rule_version":
            _require(value == (PALETTE_RULE_VERSION if kind == "color_fraction" else POSITION_RULE_VERSION),
                     "Unknown saved-feature interpretation rule.")
        elif field in ("count", "peer_count"):
            _integer(value, 0, 2147483647, field)
        elif field == "candidate_rank":
            _integer(value, 1, 2147483647, field)
        elif field == "best_distance":
            if value is not None:
                _integer(value, 0, 64, field)
        elif field == "peer_id":
            if value is not None:
                _id(value, "Duplicate peer ID")
        elif field in ("score_gap_from_best", "score_gap_to_next"):
            if value is not None:
                _number(value, 0, 2, field)
        elif field == "score":
            _number(value, -1, 1, field)
        else:
            _number(value, 0, 1, field)


def validate_cell(condition, cell, *, finalized=False):
    """Validate the complete whitelist and return detached JSON, never arbitrary payloads."""
    expected = descriptor(condition)
    fields = {"status", "raw_score", "score_type", "direction", "sources", "evidence", "reason"}
    _fields(cell, fields | ({"normalized_score"} if finalized else set()), fields)
    status = _choice(cell["status"], STATUSES, "condition status")
    _require(all(cell[key] == value for key, value in expected.items()), "Condition score descriptor differs.")
    _require(status != "not_reviewed" or (condition["kind"] == "semantic" and not finalized),
             "Only unfinished semantic decisions can be not_reviewed.")
    raw = cell["raw_score"]
    if status not in ("matched", "not_matched", "not_reviewed"):
        _require(raw is None, "Unknown or unsupported evidence cannot have a score.")
    elif expected["score_type"] == "exact":
        _require((type(raw) in (int, float) and raw == 1) if status == "matched" else raw is None,
                 "Exact predicates score one only when matched, otherwise null.")
    elif raw is not None:
        _number(raw, 0 if expected["score_type"] == "fraction" else -1, 1, "Raw score")
    _require(status != "matched" or raw is not None, "Matched conditions require a score.")
    _require(isinstance(cell["sources"], list), "Sources must be an array.")
    for source in cell["sources"]:
        _source(source)
    _evidence(condition, cell["evidence"])
    if cell["reason"] is not None:
        _text(cell["reason"], "Condition reason", limit=256)
    if "normalized_score" in cell:
        normalized = cell["normalized_score"]
        if status != "matched":
            _require(normalized is None, "Only matched conditions have normalized scores.")
        else:
            _number(normalized, 0, 1, "Normalized score")
            if expected["score_type"] == "exact":
                _require(normalized == 1, "Exact matches have normalized score one.")
    return deepcopy(cell)
