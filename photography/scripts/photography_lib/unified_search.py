"""Read-only unified retrieval and holistic, numbered, pixel-free review.

Ranking is a concatenation of semantic rank queues in original condition order,
deduplicating on first occurrence, followed by structural-only IDs in lexical
order. The first semantic queue is primary; scores from different queries or
profiles are never added or compared. AND applies all saved predicates and
requires every semantic vector before ranking, not after independent top-Ks.
Requested evidence_conditions are frozen supporting facts only; they never
participate in either operator's eligibility or the semantic rank queues.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from uuid import UUID, uuid4

from .condition_queries import _id, descriptor, validate_cell
from .condition_search import _capture_photo, _embedding_ref, _unknown
from .config import PhotographyError
from .feature_predicates import evaluate_conditions
from .fingerprints import fingerprint
from .management import rank_embedding_candidates, snapshot_scope
from .semantic_query import encode_query, prepare_query, validate_query_plan
from .unified_queries import QUERY_SCHEMA, normalize_query
from .virtual_folders import resolve_scope


SNAPSHOT_SCHEMA = "unified-search-snapshot-v1"
REVIEW_SCHEMA = "unified-search-review-v1"
RANKING_VERSION = "semantic-queues-then-structural-id-v1"
_FIELDS = {
    "schema", "schema_version", "snapshot_id", "created_at", "album", "query", "aliases",
    "scope", "scope_total", "scope_items", "profiles", "query_encodings", "coverage",
    "candidate_total", "candidate_count", "partial", "retrieval", "ranking_version",
    "candidates", "results", "stage", "selected_numbers", "selected_at", "source_digest",
    "review_id", "query_model_calls", "image_model_calls", "digest",
}
_PHOTO_FIELDS = {"photo_id", "content_version", "thumbnail_profile", "input_image_hash"}
_ROW_FIELDS = _PHOTO_FIELDS | {"conditions"}
_UNKNOWN = {"unknown_missing_index", "unknown_stale", "unknown_invalid", "unsupported"}
_STATES = _UNKNOWN | {"matched", "not_matched", "eligible", "not_ranked"}


def _require(valid, message):
    if not valid:
        raise PhotographyError("QUERY_SNAPSHOT_INVALID", message)


def _seal(snapshot):
    snapshot["digest"] = fingerprint({key: value for key, value in snapshot.items() if key != "digest"})
    return snapshot


def _same(left, right):
    return fingerprint(left) == fingerprint(right)


def _original(snapshot):
    result = deepcopy(snapshot)
    result.update(stage="candidates", selected_numbers=[], selected_at=None, source_digest=None, results=[])
    return result


def _review_id(snapshot):
    original = _original(snapshot)
    return fingerprint({"schema": REVIEW_SCHEMA, "source": {
        key: value for key, value in original.items() if key not in ("digest", "review_id")}})


class _Profiles:
    def __init__(self, profiles):
        self.profiles = profiles

    def embedding_profile(self, profile_id):
        return deepcopy(self.profiles[profile_id])

    feature_profile = embedding_profile


def _profiles(conditions, store):
    profiles = {}

    def capture(profile_id, semantic):
        if profile_id is None or profile_id in profiles:
            return
        profile = store.embedding_profile(profile_id) if semantic else store.feature_profile(profile_id)
        profiles[profile_id] = deepcopy(profile)
        if not semantic:
            for component, dependency in profile["dependencies"].items():
                capture(dependency, component == "image_embedding")

    for condition in conditions:
        capture(condition["profile_id"], condition["kind"] == "semantic")
    return profiles


def _conditions(spec):
    return spec["conditions"] + spec["evidence_conditions"]


def _supported(row, conditions, operator):
    support = [row["conditions"][condition["id"]]["status"] ==
               ("eligible" if condition["kind"] == "semantic" else "matched") for condition in conditions]
    return all(support) if operator == "and" else any(support)


def _ordered(rows, conditions, operator):
    pool = {row["photo_id"]: row for row in rows if _supported(row, conditions, operator)}
    ordered, seen = [], set()
    for condition in conditions:
        if condition["kind"] != "semantic":
            continue
        queue = sorted((row for row in pool.values()
                        if row["conditions"][condition["id"]]["status"] == "eligible"),
                       key=lambda row: (-row["conditions"][condition["id"]]["raw_score"], row["photo_id"]))
        for row in queue:
            if row["photo_id"] not in seen:
                seen.add(row["photo_id"])
                ordered.append(row)
    ordered.extend(pool[photo_id] for photo_id in sorted(pool.keys() - seen))
    return ordered


def _candidate_rows(scope_items, spec):
    rows = _ordered(scope_items, spec["conditions"], spec["operator"])
    total = len(rows)
    if spec["candidate_limit"] != "all":
        rows = rows[:spec["candidate_limit"]]
    return total, [{"number": index, **{key: deepcopy(row[key]) for key in _ROW_FIELDS}}
                   for index, row in enumerate(rows, 1)]


def _coverage(rows, conditions):
    return {condition["id"]: dict(Counter(row["conditions"][condition["id"]]["status"] for row in rows))
            for condition in conditions}


def _retrieval(rows, conditions, candidate_total, candidate_count):
    return {"unretrieved_candidate_count": candidate_total - candidate_count,
            "ineligible_photo_count": len(rows) - candidate_total,
            "semantic_eligible_counts": {
                condition["id"]: sum(row["conditions"][condition["id"]]["status"] == "eligible" for row in rows)
                for condition in conditions if condition["kind"] == "semantic"}}


def query(raw, *, store, config=None, encoder_factory=None):
    from .image_embedding import inspect_embedding
    from .image_vectors import validate_vector

    with store.read_snapshot():
        normalized = normalize_query(raw, store=store)
        spec = normalized["query"]
        conditions = _conditions(spec)
        scope, photos = resolve_scope(store=store, folder_ids=spec["scope"]["folder_ids"],
                                      folder_match=spec["scope"]["match"])
        photos = sorted(photos, key=lambda photo: photo["photo_id"])
        plans = {condition["id"]: prepare_query(condition["query"], condition.get("visual_query"))
                 for condition in conditions if condition["kind"] == "semantic"}
        structured = [condition for condition in conditions if condition["kind"] != "semantic"]
        required_structured = [condition for condition in spec["conditions"] if condition["kind"] != "semantic"]
        semantics = [condition for condition in conditions if condition["kind"] == "semantic"]
        matrix = evaluate_conditions(structured, photos, store=store)
        profiles = _profiles(conditions, store)
        captures = {}
        for condition in semantics:
            profile_id = condition["profile_id"]
            if profile_id in captures:
                continue
            captures[profile_id] = {}
            for photo in photos:
                entry, record, vector = inspect_embedding(photo, store, profiles[profile_id])
                captures[profile_id][photo["photo_id"]] = {
                    "status": entry["status"], "vector": vector,
                    "source": _embedding_ref(photo["photo_id"], record) if entry["status"] == "ready" else None}
        eligible = {photo["photo_id"] for photo in photos}
        if spec["operator"] == "and":
            eligible = {photo_id for photo_id in eligible
                        if all(matrix[photo_id][condition["id"]]["status"] == "matched"
                               for condition in required_structured)
                        and all(captures[condition["profile_id"]][photo_id]["status"] == "ready"
                                for condition in semantics)}
        rows = [{**_capture_photo(photo, store), "ingest_state": photo["ingest_state"],
                 "conditions": matrix[photo["photo_id"]]} for photo in photos]
        album = store.album()

    encoders, calls = {}, 0
    for condition in semantics:
        cid, profile_id = condition["id"], condition["profile_id"]
        captured = captures[profile_id]
        inputs = [({"photo_id": photo_id}, entry["vector"]) for photo_id, entry in captured.items()
                  if photo_id in eligible and entry["status"] == "ready"]
        scores = {}
        if inputs:
            if profile_id not in encoders:
                if encoder_factory is None:
                    from .image_embedding_profiles import default_model_dir
                    from .siglip_embedding import SiglipEncoder

                    if config is None:
                        raise PhotographyError("INVALID_ARGUMENT", "Semantic search requires config or an encoder factory.")
                    encoder = SiglipEncoder(default_model_dir(config.model_cache_root), profile=profiles[profile_id])
                else:
                    encoder = encoder_factory(profiles[profile_id])
                if fingerprint(encoder.profile()) != profile_id:
                    raise PhotographyError("INDEX_PROFILE_MISMATCH", "The query encoder differs from its frozen profile.")
                encoders[profile_id] = encoder
            encoded = encode_query(plans[cid], encoders[profile_id])
            vector = validate_vector(encoded.vector, profiles[profile_id]["dimensions"])
            calls += encoded.model_calls
            scores = {item["photo_id"]: item for item in rank_embedding_candidates(inputs, vector)}
        for row in rows:
            entry, score = captured[row["photo_id"]], scores.get(row["photo_id"])
            status = "eligible" if score else "not_ranked" if entry["status"] == "ready" else _unknown(entry["status"])
            row["conditions"][cid] = {
                "status": status, "raw_score": score["score"] if score else None, **descriptor(condition),
                "sources": [entry["source"]] if entry["source"] else [],
                "evidence": {key: score[key] for key in
                             ("candidate_rank", "score_gap_from_best", "score_gap_to_next")} if score else {},
                "reason": None if score else "local_prefilter" if status == "not_ranked" else entry["status"]}
    total, candidates = _candidate_rows(rows, spec)
    snapshot = {
        "schema": SNAPSHOT_SCHEMA, "schema_version": 1, "snapshot_id": "unified_snapshot_" + uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(), "album": album, "query": spec,
        "aliases": normalized["aliases"], "scope": scope, "scope_total": len(rows), "scope_items": rows,
        "profiles": profiles, "query_encodings": plans, "coverage": _coverage(rows, conditions),
        "candidate_total": total, "candidate_count": len(candidates), "partial": len(candidates) < total,
        "retrieval": _retrieval(rows, conditions, total, len(candidates)), "ranking_version": RANKING_VERSION,
        "candidates": candidates, "results": [], "stage": "candidates", "selected_numbers": [],
        "selected_at": None, "source_digest": None, "review_id": None, "query_model_calls": calls,
        "image_model_calls": 0,
    }
    snapshot["review_id"] = _review_id(snapshot)
    return _seal(snapshot)


def _validate_semantic(condition, cell, photo_id):
    status = cell["status"]
    _require(status in _UNKNOWN | {"eligible", "not_ranked"}, "Semantic cells are relevance, not match decisions.")
    checked = deepcopy(cell)
    if status in ("eligible", "not_ranked"):
        checked["status"] = "not_reviewed"
    validate_cell(condition, checked)
    if status in ("eligible", "not_ranked"):
        _require(len(cell["sources"]) == 1 and cell["sources"][0]["kind"] == "embedding"
                 and cell["sources"][0]["photo_id"] == photo_id
                 and cell["sources"][0]["profile_id"] == condition["profile_id"],
                 "Semantic evidence must identify its own frozen embedding.")
    else:
        _require(cell["sources"] == [] and cell["evidence"] == {}, "Unknown semantic cells cannot invent evidence.")
    if status == "eligible":
        _require(type(cell["raw_score"]) in (int, float) and math.isfinite(cell["raw_score"])
                 and cell["reason"] is None and set(cell["evidence"]) ==
                 {"candidate_rank", "score_gap_from_best", "score_gap_to_next"},
                 "Eligible semantic candidates need full numerical evidence.")
    elif status == "not_ranked":
        _require(cell["raw_score"] is None and cell["evidence"] == {} and cell["reason"] == "local_prefilter",
                 "Locally filtered candidates must not have semantic scores.")


def _validate_numbers(numbers, count, *, allowed=None):
    if (not isinstance(numbers, list) or any(type(number) is not int or not 1 <= number <= count for number in numbers)
            or len(set(numbers)) != len(numbers) or allowed is not None and not set(numbers) <= set(allowed)):
        raise PhotographyError("INVALID_ARGUMENT", "Select unique offered integer numbers; [] selects nothing.")
    return sorted(numbers)


def _validate_snapshot(snapshot, store=None):
    try:
        _require(isinstance(snapshot, dict) and set(snapshot) == _FIELDS, "Expected a complete unified snapshot.")
        json.dumps(snapshot, allow_nan=False)
        _require(snapshot["schema"] == SNAPSHOT_SCHEMA and type(snapshot["schema_version"]) is int
                 and snapshot["schema_version"] == 1, "Unsupported unified snapshot version.")
        _id(snapshot["snapshot_id"])
        datetime.fromisoformat(snapshot["created_at"])
        _require(snapshot["stage"] in ("candidates", "selected"), "Invalid unified snapshot stage.")
        _require(isinstance(snapshot["album"], dict) and set(snapshot["album"]) ==
                 {"id", "name", "database_path", "created_at"} and
                 all(isinstance(value, str) for value in snapshot["album"].values()), "Invalid frozen album identity.")
        UUID(snapshot["album"]["id"])
        if store is not None and UUID(snapshot["album"]["id"]) != UUID(store.album()["id"]):
            raise PhotographyError("ALBUM_MISMATCH", "This unified snapshot belongs to another album.")
        _require(snapshot["digest"] == fingerprint({key: value for key, value in snapshot.items() if key != "digest"}),
                 "Unified snapshot integrity changed.")
        _require(snapshot["review_id"] == _review_id(snapshot), "Numbered review identity changed.")
        profiles = snapshot["profiles"]
        _require(isinstance(profiles, dict) and all(fingerprint(value) == key for key, value in profiles.items()),
                 "Frozen profile identities changed.")
        frozen_store = _Profiles(profiles)
        spec = normalize_query(snapshot["query"], store=frozen_store)["query"]
        _require(spec == snapshot["query"], "Unified query must have frozen canonical profiles and predicates.")
        requested = _conditions(spec)
        conditions = {condition["id"]: condition for condition in requested}
        _require(_profiles(requested, frozen_store) == profiles, "Unexpected frozen profiles.")
        if store is not None:
            _require(_profiles(requested, store) == profiles, "Saved profiles differ from this snapshot.")
        _require(isinstance(snapshot["aliases"], dict)
                 and all(_id(key) and value in conditions for key, value in snapshot["aliases"].items())
                 and all(snapshot["aliases"].get(cid) == cid for cid in conditions), "Invalid condition aliases.")
        plans = snapshot["query_encodings"]
        _require(isinstance(plans, dict) and set(plans) ==
                 {cid for cid, condition in conditions.items() if condition["kind"] == "semantic"},
                 "Missing frozen semantic recipes.")
        for cid, plan in plans.items():
            _require(validate_query_plan(plan) == prepare_query(conditions[cid]["query"],
                     conditions[cid].get("visual_query")), "Frozen English query recipe changed.")
        scope = snapshot_scope(snapshot)
        _require(scope == snapshot["scope"], "Unexpected historical scope fields.")
        if spec["scope"]["folder_ids"]:
            _require(scope["kind"] == "virtual_folders" and scope["match"] == spec["scope"]["match"]
                     and [folder["folder_id"] for folder in scope["folders"]] == spec["scope"]["folder_ids"],
                     "Frozen folder scope differs from the request.")
        else:
            _require(scope["kind"] == "album", "Frozen scope differs from the request.")
        rows = snapshot["scope_items"]
        _require(isinstance(rows, list) and type(snapshot["scope_total"]) is int
                 and len(rows) == snapshot["scope_total"], "Invalid frozen scope count.")
        previous = None
        for row in rows:
            _require(isinstance(row, dict) and set(row) == _ROW_FIELDS | {"ingest_state"},
                     "Unexpected private photo fields.")
            for key in _PHOTO_FIELDS - {"input_image_hash"}:
                _id(row[key])
            if row["input_image_hash"] is not None:
                _id(row["input_image_hash"])
            _require(row["ingest_state"] in ("available", "error")
                     and (previous is None or row["photo_id"] > previous)
                     and isinstance(row["conditions"], dict) and set(row["conditions"]) == set(conditions),
                     "Frozen photo IDs must be unique and condition cells complete.")
            previous = row["photo_id"]
            for cid, condition in conditions.items():
                cell = row["conditions"][cid]
                if condition["kind"] == "semantic":
                    _validate_semantic(condition, cell, row["photo_id"])
                else:
                    validate_cell(condition, cell)
                    _require(cell["status"] != "not_reviewed", "Saved predicates cannot require semantic review.")
        scoped_ids = {row["photo_id"] for row in rows}
        for row in rows:
            for cid, condition in conditions.items():
                cell = row["conditions"][cid]
                duplicate = condition["kind"] == "has_near_duplicate"
                source_kind = ("embedding" if condition["kind"] == "semantic" else
                               "photo" if duplicate and condition["metric"] == "exact" else "feature")
                for source in cell["sources"]:
                    _require(source["kind"] == source_kind and source["photo_id"] in scoped_ids
                             and (duplicate or source["photo_id"] == row["photo_id"])
                             and (source_kind == "photo" or source["profile_id"] == condition["profile_id"]),
                             "Condition cells refer to an unrelated saved input.")
                if condition["kind"] != "semantic" and cell["status"] in ("matched", "not_matched"):
                    _require(sum(source["photo_id"] == row["photo_id"] for source in cell["sources"]) == 1,
                             "A saved predicate requires its own evidence source.")
                    if duplicate and cell["status"] == "matched":
                        evidence = cell["evidence"]
                        _require(evidence["peer_id"] in scoped_ids and evidence["peer_id"] != row["photo_id"]
                                 and evidence["peer_count"] > 0 and evidence["best_distance"] is not None
                                 and evidence["best_distance"] <= condition["max_distance"]
                                 and len(cell["sources"]) == 2
                                 and any(source["photo_id"] == evidence["peer_id"] for source in cell["sources"]),
                                 "Duplicate matches need an in-scope witness.")
        for cid in plans:
            ordered = sorted((row for row in rows if row["conditions"][cid]["status"] == "eligible"),
                             key=lambda row: (-row["conditions"][cid]["raw_score"], row["photo_id"]))
            for index, row in enumerate(ordered):
                cell, evidence = row["conditions"][cid], row["conditions"][cid]["evidence"]
                _require(evidence["candidate_rank"] == index + 1 and math.isclose(
                    evidence["score_gap_from_best"], ordered[0]["conditions"][cid]["raw_score"] - cell["raw_score"],
                    abs_tol=1e-12), "Semantic ranks or best-score gaps changed.")
                following = ordered[index + 1]["conditions"][cid]["raw_score"] if index + 1 < len(ordered) else None
                _require(evidence["score_gap_to_next"] is None if following is None else
                         type(evidence["score_gap_to_next"]) in (int, float) and math.isclose(
                             evidence["score_gap_to_next"], cell["raw_score"] - following, abs_tol=1e-12),
                         "Semantic next-score gaps changed.")
        total, candidates = _candidate_rows(rows, spec)
        _require(type(snapshot["candidate_count"]) is int and type(snapshot["candidate_total"]) is int
                 and snapshot["candidate_total"] == total and snapshot["candidate_count"] == len(candidates)
                 and _same(snapshot["candidates"], candidates) and type(snapshot["partial"]) is bool
                 and snapshot["partial"] == (len(candidates) < total), "Global candidate mapping changed.")
        _require(_same(snapshot["coverage"], _coverage(rows, requested)) and
                 _same(snapshot["retrieval"], _retrieval(rows, requested, total, len(candidates)))
                 and snapshot["ranking_version"] == RANKING_VERSION, "Retrieval aggregates changed.")
        _require(type(snapshot["query_model_calls"]) is int and
                 snapshot["query_model_calls"] == sum(bool(snapshot["retrieval"]["semantic_eligible_counts"][cid])
                                                       for cid in plans)
                 and type(snapshot["image_model_calls"]) is int and snapshot["image_model_calls"] == 0,
                 "Invalid inference accounting.")
        numbers = _validate_numbers(snapshot["selected_numbers"], len(candidates))
        _require(numbers == snapshot["selected_numbers"], "Selections must preserve frozen candidate order.")
        if snapshot["stage"] == "candidates":
            _require(numbers == [] and snapshot["results"] == [] and snapshot["source_digest"] is None
                     and snapshot["selected_at"] is None, "Candidate snapshots cannot claim selections.")
        else:
            datetime.fromisoformat(snapshot["selected_at"])
            _require(_seal(_original(snapshot))["digest"] == snapshot["source_digest"],
                     "Selected snapshot lost its original source binding.")
            chosen = set(numbers)
            _require(_same(snapshot["results"], [row for row in candidates if row["number"] in chosen]),
                     "Selected rows differ from the exact numbered decision.")
    except PhotographyError as exc:
        if exc.code in ("ALBUM_MISMATCH", "QUERY_SNAPSHOT_INVALID"):
            raise
        raise PhotographyError("QUERY_SNAPSHOT_INVALID", "Invalid frozen unified-query evidence.",
                               details={"cause": exc.code}) from exc
    except (TypeError, KeyError, ValueError, OverflowError, AttributeError) as exc:
        raise PhotographyError("QUERY_SNAPSHOT_INVALID", "Malformed unified snapshot.") from exc
    return snapshot


def _validate_current(snapshot, store, *, rows=None):
    """Recheck reviewed rows and explicit witnesses, without querying the whole scope again."""
    from .feature_index import inspect_feature
    from .feature_predicates import _coverage as feature_coverage, _feature_source
    from .image_embedding import inspect_embedding

    try:
        rows = snapshot["candidates"] if rows is None else rows
        photos = [store.photo(row["photo_id"]) for row in rows]
        conditions = _conditions(snapshot["query"])
        structured = [condition for condition in conditions if condition["kind"] not in ("semantic", "has_near_duplicate")]
        matrix = evaluate_conditions(structured, photos, store=store)
        checked, witnesses = {}, {}
        frozen_photos = {row["photo_id"]: row for row in snapshot["scope_items"]}
        for row, photo in zip(rows, photos):
            if (_capture_photo(photo, store) != {key: row[key] for key in _PHOTO_FIELDS}
                    or photo["ingest_state"] != frozen_photos[row["photo_id"]]["ingest_state"]):
                raise PhotographyError("QUERY_SNAPSHOT_STALE", "A captured photo or preview identity changed.")
            for condition in conditions:
                cid, cell = condition["id"], row["conditions"][condition["id"]]
                if condition["kind"] == "has_near_duplicate":
                    if condition["metric"] == "hamming" and not cell["sources"]:
                        entry, record = inspect_feature(
                            row["photo_id"], snapshot["profiles"][condition["profile_id"]], store=store)
                        if feature_coverage(entry, record) != (cell["status"], cell["reason"]):
                            raise PhotographyError("QUERY_SNAPSHOT_STALE", "A frozen duplicate input's availability changed.")
                    for source in cell["sources"]:
                        key = fingerprint(source)
                        if key not in witnesses:
                            if source["kind"] == "photo":
                                current = store.photo(source["photo_id"])
                                witnesses[key] = (all(current[field] == source[field]
                                                      for field in ("content_version", "ingest_state")), None)
                            else:
                                entry, record = inspect_feature(
                                    source["photo_id"], snapshot["profiles"][source["profile_id"]], store=store)
                                witnesses[key] = (record is not None and _feature_source(record) == source,
                                                  feature_coverage(entry, record))
                        same_source, unknown = witnesses[key]
                        required_ready = cell["status"] in ("matched", "not_matched") or cell["reason"] == "incomplete_peer_coverage"
                        same_status = unknown is None if required_ready else unknown == (cell["status"], cell["reason"])
                        if not same_source or not same_status:
                            raise PhotographyError("QUERY_SNAPSHOT_STALE", "A frozen duplicate input or witness changed.")
                    continue
                if condition["kind"] != "semantic":
                    if matrix[row["photo_id"]][cid] != cell:
                        raise PhotographyError("QUERY_SNAPSHOT_STALE", "Saved predicate evidence or a dependency changed.")
                    continue
                key = (row["photo_id"], condition["profile_id"])
                if key not in checked:
                    checked[key] = inspect_embedding(photo, store, snapshot["profiles"][condition["profile_id"]])
                entry, record, _ = checked[key]
                if cell["status"] in ("eligible", "not_ranked"):
                    valid = entry["status"] == "ready" and cell["sources"] == [_embedding_ref(row["photo_id"], record)]
                else:
                    valid = entry["status"] != "ready" and _unknown(entry["status"]) == cell["status"]
                if not valid:
                    raise PhotographyError("QUERY_SNAPSHOT_STALE", "A captured embedding identity or availability changed.")
    except PhotographyError as exc:
        if exc.code == "QUERY_SNAPSHOT_STALE":
            raise
        raise PhotographyError("QUERY_SNAPSHOT_STALE", "A frozen query input is no longer available.",
                               details={"cause": exc.code}) from exc


def _compact_condition(condition, spaces, *, supporting=False):
    kind = condition["kind"]
    predicate = {key: deepcopy(value) for key, value in condition.items()
                 if key not in ("id", "kind", "profile_id", "scoring")}
    result = {"id": condition["id"], "kind": kind, "predicate": predicate,
              "scoring": {**descriptor(condition), "role": "supporting_evidence" if supporting else
                          "soft_relevance" if kind == "semantic" else "saved_predicate"}}
    if kind == "semantic":
        result["scoring"]["space"] = spaces[condition["profile_id"]]
    return result


def _rounded(value):
    return round(value, 6) if type(value) is float else value


def _compact_cell(condition, cell):
    result = {"status": cell["status"]}
    kind = condition["kind"]
    if kind == "semantic" and cell["status"] == "eligible":
        result.update(score=_rounded(cell["raw_score"]), rank=cell["evidence"]["candidate_rank"],
                      gap_from_best=_rounded(cell["evidence"]["score_gap_from_best"]),
                      gap_to_next=_rounded(cell["evidence"]["score_gap_to_next"]))
    elif kind == "ocr_contains":
        result["hit"] = cell["status"] == "matched" if cell["status"] in ("matched", "not_matched") else None
    else:
        result.update({key: _rounded(value) for key, value in cell["evidence"].items()
                       if key not in ("snippet", "peer_id")})
    return result


def review(snapshot, *, store):
    with store.read_snapshot():
        _validate_snapshot(snapshot, store)
        if snapshot["stage"] != "candidates":
            raise PhotographyError("QUERY_STAGE_INVALID", "Review an original unified candidate snapshot.")
    conditions = _conditions(snapshot["query"])
    spaces = {}
    for condition in conditions:
        if condition["kind"] == "semantic":
            spaces.setdefault(condition["profile_id"], len(spaces) + 1)
    return {"schema": REVIEW_SCHEMA, "review_id": snapshot["review_id"], "query": snapshot["query"]["query"],
            "operator": snapshot["query"]["operator"],
            "conditions": [_compact_condition(condition, spaces) for condition in snapshot["query"]["conditions"]],
            "evidence_conditions": [_compact_condition(condition, spaces, supporting=True)
                                    for condition in snapshot["query"]["evidence_conditions"]],
            "coverage": deepcopy(snapshot["coverage"]), "scope_total": snapshot["scope_total"],
            "candidate_total": snapshot["candidate_total"], "candidate_count": snapshot["candidate_count"],
            "partial": snapshot["partial"], "ranking_version": RANKING_VERSION,
            "model_calls": 0, "query_model_calls": snapshot["query_model_calls"], "image_model_calls": 0,
            "candidates": [{"id": row["number"], "conditions": {
                condition["id"]: _compact_cell(condition, row["conditions"][condition["id"]])
                for condition in conditions}} for row in snapshot["candidates"]],
            "instruction": "Review the whole request using only this evidence. Return only an integer-number array; "
                           "[] is valid. Semantic scores are relevance, not verified matches. Unknown is not false."}


def _check_review(snapshot, review_id):
    if not isinstance(review_id, str) or review_id != snapshot["review_id"]:
        raise PhotographyError("QUERY_REVIEW_MISMATCH", "Supply the explicit review ID for this numbered candidate set.")


def select(snapshot, numbers, *, review_id, store):
    with store.read_snapshot():
        _validate_snapshot(snapshot, store)
        if snapshot["stage"] != "candidates":
            raise PhotographyError("QUERY_STAGE_INVALID", "Select from an original unified candidate snapshot.")
        _check_review(snapshot, review_id)
        chosen = _validate_numbers(numbers, snapshot["candidate_count"])
        _validate_current(snapshot, store)
        result = deepcopy(snapshot)
        chosen_set = set(chosen)
        result.update(stage="selected", selected_numbers=chosen, source_digest=snapshot["digest"],
                      selected_at=datetime.now(timezone.utc).isoformat(),
                      results=[deepcopy(row) for row in snapshot["candidates"] if row["number"] in chosen_set])
        return _seal(result)


def summary(snapshot, *, store=None, model_calls=0):
    if store is None:
        _validate_snapshot(snapshot)
    else:
        with store.read_snapshot():
            _validate_snapshot(snapshot, store)
    return {"schema": "unified-search-summary-v1", "snapshot_id": snapshot["snapshot_id"],
            "review_id": snapshot["review_id"], "stage": snapshot["stage"],
            "scope_total": snapshot["scope_total"], "candidate_total": snapshot["candidate_total"],
            "candidate_count": snapshot["candidate_count"], "candidate_limit": snapshot["query"]["candidate_limit"],
            "partial": snapshot["partial"], "selected_count": len(snapshot["selected_numbers"]),
            "selected_numbers": deepcopy(snapshot["selected_numbers"]), "model_calls": model_calls,
            "image_model_calls": 0, "original_verification": "not_checked"}


def selected_rows(snapshot, *, store):
    with store.read_snapshot():
        _validate_snapshot(snapshot, store)
        if snapshot["stage"] != "selected":
            raise PhotographyError("QUERY_STAGE_INVALID", "Select reviewed numbers before showing local results.")
        _validate_current(snapshot, store, rows=snapshot["results"])
        return deepcopy(snapshot["results"])


def select_for_folder(snapshot, numbers, *, review_id, store):
    with store.read_snapshot():
        _validate_snapshot(snapshot, store)
        if snapshot["stage"] != "selected":
            raise PhotographyError("QUERY_STAGE_INVALID", "Only previously selected numbers may enter a folder.")
        _check_review(snapshot, review_id)
        chosen = _validate_numbers(numbers, snapshot["candidate_count"], allowed=snapshot["selected_numbers"])
        chosen_set = set(chosen)
        rows = [row for row in snapshot["results"] if row["number"] in chosen_set]
        _validate_current(snapshot, store, rows=rows)
        return {"snapshot_id": snapshot["snapshot_id"], "snapshot_digest": snapshot["digest"],
                "source_digest": snapshot["source_digest"], "review_id": snapshot["review_id"],
                "photo_ids": [row["photo_id"] for row in rows],
                "selected_numbers": chosen, "selected_count": len(chosen), "result_count": len(snapshot["results"]),
                "scope": deepcopy(snapshot["scope"]), "method": "explicit_unified_selected_numbers",
                "model_calls": 0, "image_model_calls": 0}
