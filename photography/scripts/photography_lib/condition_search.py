"""Read-only OR retrieval, isolated numerical review and self-contained historical snapshots."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from uuid import UUID, uuid4

from .condition_queries import descriptor, normalize_query, validate_cell
from .condition_ranking import rank_results, validate_ranking
from .config import PhotographyError
from .feature_predicates import evaluate_conditions
from .fingerprints import fingerprint
from .management import rank_embedding_candidates, snapshot_scope
from .semantic_query import encode_query, prepare_query, validate_query_plan
from .virtual_folders import resolve_scope


SCHEMA = "condition-search-snapshot-v1"
STATES = ("matched", "not_matched", "unknown_missing_index", "unknown_stale", "unknown_invalid",
          "unsupported", "not_reviewed")
_FIELDS = {
    "schema", "schema_version", "snapshot_id", "created_at", "album", "query", "aliases", "scope",
    "scope_total", "coverage", "candidate_count", "retrieval", "candidates", "results", "stage",
    "query_model_calls", "image_model_calls", "random_seed", "ranking_version", "normalization",
    "decisions", "finalized_at", "source_digest", "digest",
    "evaluated_coverage",
}
_ROW_FIELDS = {"photo_id", "content_version", "thumbnail_profile", "input_image_hash", "conditions"}
_RANK_FIELDS = {"matched_condition_ids", "matched_count", "pattern_score", "result_rank"}


def _require(condition, message):
    if not condition:
        raise PhotographyError("QUERY_SNAPSHOT_INVALID", message)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _seal(snapshot):
    snapshot["digest"] = fingerprint({key: value for key, value in snapshot.items() if key != "digest"})
    return snapshot


def _unknown(status):
    return ("unknown_missing_index" if status == "missing" else
            "unknown_stale" if status == "stale" else "unknown_invalid")


def _embedding_ref(photo_id, record):
    return {"kind": "embedding", "photo_id": photo_id, **{
        key: record[key] for key in ("profile_id", "result_id", "content_version", "thumbnail_profile",
                                    "input_image_hash", "vector_hash")}}


def _capture_photo(photo, store):
    image_hash = None
    try:
        thumbnail = store.thumbnail(photo["photo_id"], include_data=False)
        if (thumbnail["content_version"], thumbnail["profile"]) == (photo["content_version"], photo["thumbnail_profile"]):
            image_hash = thumbnail["image_hash"]
    except PhotographyError as exc:
        if exc.code != "INVALID_PREVIEW":
            raise
    return {key: photo[key] for key in ("photo_id", "content_version", "thumbnail_profile")} | {
        "input_image_hash": image_hash}


def query(raw_query, *, store, config=None, encoder_factory=None):
    from .image_embedding import inspect_embedding
    from .image_vectors import validate_vector

    captures = {}
    with store.read_snapshot():
        normalized = normalize_query(raw_query, store=store)
        spec = normalized["query"]
        scope, photos = resolve_scope(store=store, folder_ids=spec["scope"]["folder_ids"],
                                      folder_match=spec["scope"]["match"])
        photo_map = {photo["photo_id"]: _capture_photo(photo, store) for photo in photos}
        conditions = spec["conditions"]
        query_plans = {condition["id"]: prepare_query(condition["query"], condition.get("visual_query"))
                       for condition in conditions if condition["kind"] == "semantic"}
        structured = [condition for condition in conditions if condition["kind"] != "semantic"]
        matrix = evaluate_conditions(structured, photos, store=store)
        for condition in conditions:
            if condition["kind"] != "semantic" or condition["profile_id"] in captures:
                continue
            pid = condition["profile_id"]
            profile = store.embedding_profile(pid)
            candidates, states, sources = [], {}, {}
            for photo in photos:
                entry, record, vector = inspect_embedding(photo, store, profile)
                photo_id = photo["photo_id"]
                states[photo_id] = entry["status"]
                if entry["status"] == "ready":
                    candidates.append(({"photo_id": photo_id}, vector))
                    sources[photo_id] = _embedding_ref(photo_id, record)
            captures[pid] = {"profile": profile, "candidates": candidates, "states": states, "sources": sources}
        album = store.album()

    pool = {photo_id for photo_id, cells in matrix.items() if any(cell["status"] == "matched" for cell in cells.values())}
    ranked_by_condition, coverage, calls = {}, {}, 0
    encoders = {}
    for condition in structured:
        coverage[condition["id"]] = dict(Counter(matrix[photo["photo_id"]][condition["id"]]["status"] for photo in photos))
    for condition in conditions:
        if condition["kind"] != "semantic":
            continue
        captured = captures[condition["profile_id"]]
        ranked = []
        if captured["candidates"]:
            if condition["profile_id"] not in encoders:
                if encoder_factory is None:
                    from .image_embedding_profiles import default_model_dir
                    from .siglip_embedding import SiglipEncoder

                    if config is None:
                        raise PhotographyError("INVALID_ARGUMENT", "Semantic conditions require config or an encoder factory.")
                    encoder = SiglipEncoder(default_model_dir(config.model_cache_root), profile=captured["profile"])
                else:
                    encoder = encoder_factory(captured["profile"])
                encoders[condition["profile_id"]] = encoder
            encoder = encoders[condition["profile_id"]]
            if fingerprint(encoder.profile()) != condition["profile_id"]:
                raise PhotographyError("INDEX_PROFILE_MISMATCH", "Query encoder does not match the frozen condition profile.")
            encoded = encode_query(query_plans[condition["id"]], encoder)
            vector = validate_vector(encoded.vector, captured["profile"]["dimensions"])
            calls += encoded.model_calls
            ranked = rank_embedding_candidates(captured["candidates"], vector)
        maximum = len(ranked) if spec["semantic_candidates"] == "all" else spec["semantic_candidates"]
        pool.update(item["photo_id"] for item in ranked[:maximum])
        ranked_by_condition[condition["id"]] = {item["photo_id"]: item for item in ranked}
        coverage[condition["id"]] = dict(Counter(
            "not_reviewed" if status == "ready" else _unknown(status) for status in captured["states"].values()))

    rows = []
    for photo_id in sorted(pool):
        cells = deepcopy(matrix.get(photo_id, {}))
        for condition in conditions:
            if condition["kind"] != "semantic":
                continue
            captured = captures[condition["profile_id"]]
            score = ranked_by_condition[condition["id"]].get(photo_id)
            cell = {"status": "not_reviewed" if score else _unknown(captured["states"][photo_id]),
                    "raw_score": score["score"] if score else None, **descriptor(condition),
                    "sources": [captured["sources"][photo_id]] if score else [],
                    "evidence": {key: score[key] for key in
                                 ("candidate_rank", "score_gap_from_best", "score_gap_to_next")} if score else {},
                    "reason": None if score else captured["states"][photo_id]}
            cells[condition["id"]] = validate_cell(condition, cell)
        rows.append({**photo_map[photo_id], "conditions": cells})
    limited = any(len(scores) > sum(row["photo_id"] in scores for row in rows)
                  for scores in ranked_by_condition.values())
    snapshot = _seal({
        "schema": SCHEMA, "schema_version": 1, "snapshot_id": "condition_snapshot_" + uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(), "album": album,
        "query": spec, "aliases": normalized["aliases"], "scope": scope, "scope_total": len(photos),
        "coverage": coverage, "candidate_count": len(rows),
        "retrieval": {"semantic_retrieval_limited": limited, "unretrieved_photo_count": len(photos) - len(rows),
                      "semantic_eligible_counts": {key: len(value) for key, value in ranked_by_condition.items()}},
        "candidates": rows, "results": [], "stage": "awaiting_semantic_decisions",
        "query_model_calls": calls, "image_model_calls": 0, "random_seed": spec["random_seed"] or uuid4().hex,
        "query_encodings": query_plans,
        "ranking_version": None, "normalization": {}, "decisions": None,
        "finalized_at": None, "source_digest": None, "evaluated_coverage": {},
    })
    if not any(row["conditions"][condition["id"]]["status"] == "not_reviewed"
               for row in rows for condition in conditions):
        return _finalize_body(snapshot, {"schema": "condition-decisions-v1",
                                        "snapshot_id": snapshot["snapshot_id"], "pages": []})
    return snapshot


def _review_rows(snapshot, condition_id):
    return sorted((row for row in snapshot["candidates"] if row["conditions"][condition_id]["status"] == "not_reviewed"),
                  key=lambda row: (-row["conditions"][condition_id]["raw_score"], row["photo_id"]))


def _page_evidence(snapshot, condition, page, *, rows=None):
    if rows is None:
        rows = _review_rows(snapshot, condition["id"])
    size = snapshot["query"]["review_page_size"]
    total_pages = (len(rows) + size - 1) // size
    if type(page) is not int or not 0 <= page < total_pages:
        raise PhotographyError("INVALID_ARGUMENT", "Choose an existing semantic evidence page.")
    selected = rows[page * size:(page + 1) * size]
    items = []
    for row in selected:
        cell = row["conditions"][condition["id"]]
        source = cell["sources"][0]
        items.append({"photo_id": row["photo_id"], "score": cell["raw_score"],
                      **{key: source[key] for key in ("profile_id", "result_id", "content_version",
                                                     "thumbnail_profile", "input_image_hash", "vector_hash")},
                      **{key: cell["evidence"][key] for key in
                         ("candidate_rank", "score_gap_from_best", "score_gap_to_next")}})
    identity = {"snapshot_id": snapshot["snapshot_id"], "condition_id": condition["id"],
                "query": condition["query"], "page": page, "items": items}
    if "query_encodings" in snapshot:
        identity["query_encoding"] = deepcopy(snapshot["query_encodings"][condition["id"]])
    return {"schema": "condition-semantic-evidence-v1", **identity,
            "page_id": fingerprint(identity), "page_count": total_pages, "candidate_count": len(rows),
            "selection_evidence": "embedding_similarity_only", "model_calls": 0, "image_model_calls": 0}


def _resolve_decisions(snapshot, decisions):
    _require(isinstance(decisions, dict) and set(decisions) == {"schema", "snapshot_id", "pages"}
             and decisions["schema"] == "condition-decisions-v1"
             and decisions["snapshot_id"] == snapshot["snapshot_id"] and isinstance(decisions["pages"], list),
             "Decisions must identify this snapshot and explicit reviewed pages.")
    required = {}
    for condition in snapshot["query"]["conditions"]:
        if condition["kind"] != "semantic":
            continue
        rows = _review_rows(snapshot, condition["id"])
        count = len(rows)
        size = snapshot["query"]["review_page_size"]
        for page in range((count + size - 1) // size):
            evidence = _page_evidence(snapshot, condition, page, rows=rows)
            required[evidence["page_id"]] = (condition["id"], {item["photo_id"] for item in evidence["items"]})
    reviewed, chosen = set(), {}
    for page in decisions["pages"]:
        _require(isinstance(page, dict) and set(page) == {"condition_id", "page_id", "matched_photo_ids"}
                 and _text(page["page_id"]) and _text(page["condition_id"])
                 and isinstance(page["matched_photo_ids"], list)
                 and all(_text(pid) for pid in page["matched_photo_ids"]),
                 "Invalid semantic decision page.")
        key = page["page_id"]
        _require(key in required and key not in reviewed, "Unknown or duplicate semantic review page.")
        condition_id, offered = required[key]
        _require(condition_id == page["condition_id"] and set(page["matched_photo_ids"]) <= offered,
                 "Selection must be a subset of this condition's evidence page.")
        reviewed.add(key)
        chosen.setdefault(condition_id, set()).update(page["matched_photo_ids"])
    _require(reviewed == set(required), "Review all required semantic pages before finalizing match counts.")
    rows = deepcopy(snapshot["candidates"])
    for row in rows:
        for condition_id, cell in row["conditions"].items():
            if cell["status"] == "not_reviewed":
                cell["status"] = "matched" if row["photo_id"] in chosen.get(condition_id, set()) else "not_matched"
                cell["reason"] = "agent_selected" if cell["status"] == "matched" else "agent_rejected"
    return rows


def _validate_snapshot(snapshot, store):
    _require(isinstance(snapshot, dict) and set(snapshot) in (_FIELDS, _FIELDS | {"query_encodings"}),
             "Expected a complete condition-search snapshot.")
    try:
        json.dumps(snapshot, allow_nan=False)
        _require(snapshot["schema"] == SCHEMA and type(snapshot["schema_version"]) is int
                 and snapshot["schema_version"] == 1 and _text(snapshot["snapshot_id"]),
                 "Unsupported condition snapshot version or identity.")
        _require(snapshot["stage"] in ("awaiting_semantic_decisions", "finalized"), "Invalid snapshot stage.")
        _require(isinstance(snapshot["album"], dict)
                 and set(snapshot["album"]) == {"id", "name", "database_path", "created_at"}
                 and all(isinstance(value, str) for value in snapshot["album"].values())
                 and _text(snapshot["album"]["id"]), "Snapshot must identify its album using scalar metadata.")
        if UUID(snapshot["album"]["id"]) != UUID(store.album()["id"]):
            raise PhotographyError("ALBUM_MISMATCH", "The condition snapshot belongs to another album.")
        _require(fingerprint({key: value for key, value in snapshot.items() if key != "digest"}) == snapshot["digest"],
                 "Condition snapshot digest is invalid.")
        canonical = normalize_query(snapshot["query"], store=store)["query"]
        _require(canonical == snapshot["query"], "Snapshot query must be canonical with frozen profiles.")
        conditions = {condition["id"]: condition for condition in canonical["conditions"]}
        if "query_encodings" in snapshot:
            plans = snapshot["query_encodings"]
            _require(isinstance(plans, dict) and set(plans) == {
                cid for cid, condition in conditions.items() if condition["kind"] == "semantic"},
                "Each semantic condition needs exactly one frozen query recipe.")
            for cid, plan in plans.items():
                _require(validate_query_plan(plan) == prepare_query(
                    conditions[cid]["query"], conditions[cid].get("visual_query")),
                    "Query preparation disagrees with the frozen condition.")
        else:
            _require(not any("visual_query" in condition for condition in conditions.values()),
                     "Prepared visual queries require their frozen recipe metadata.")
        _require(isinstance(snapshot["aliases"], dict)
                 and all(_text(key) and value in conditions for key, value in snapshot["aliases"].items())
                 and all(snapshot["aliases"].get(key) == key for key in conditions), "Invalid condition aliases.")
        scope = snapshot_scope(snapshot)
        _require(scope == snapshot["scope"], "Snapshot scope contains unexpected fields.")
        folder_ids = canonical["scope"]["folder_ids"]
        if folder_ids:
            _require(scope["kind"] == "virtual_folders"
                     and [folder["folder_id"] for folder in scope["folders"]] == folder_ids
                     and scope["match"] == (canonical["scope"]["match"] or "union"), "Query/snapshot folder scope mismatch.")
        else:
            _require(scope["kind"] == "album", "Query/snapshot album scope mismatch.")
        _require(type(snapshot["scope_total"]) is int and snapshot["scope_total"] >= 0
                 and type(snapshot["candidate_count"]) is int
                 and isinstance(snapshot["candidates"], list)
                 and len(snapshot["candidates"]) == snapshot["candidate_count"] <= snapshot["scope_total"],
                 "Invalid candidate counts.")
        _require(type(snapshot["query_model_calls"]) is int and snapshot["query_model_calls"] >= 0
                 and type(snapshot["image_model_calls"]) is int and snapshot["image_model_calls"] == 0
                 and _text(snapshot["random_seed"]) and len(snapshot["random_seed"]) <= 256,
                 "Invalid model-call or seed metadata.")
        if canonical["random_seed"] is not None:
            _require(snapshot["random_seed"] == canonical["random_seed"], "Random seed does not match the query.")
        previous = None
        candidate_ids = {row["photo_id"] for row in snapshot["candidates"]}
        for row in snapshot["candidates"]:
            _require(isinstance(row, dict) and set(row) == _ROW_FIELDS
                     and all(_text(row[key]) for key in ("photo_id", "content_version", "thumbnail_profile"))
                     and (row["input_image_hash"] is None or _text(row["input_image_hash"]))
                     and isinstance(row["conditions"], dict) and set(row["conditions"]) == set(conditions)
                     and (previous is None or row["photo_id"] > previous), "Invalid or duplicate candidate row.")
            previous = row["photo_id"]
            for cid, condition in conditions.items():
                cell = validate_cell(condition, row["conditions"][cid])
                _require(cell == row["conditions"][cid], "Noncanonical condition evidence.")
                if condition["kind"] == "semantic":
                    _require(cell["status"] not in ("matched", "not_matched"),
                             "Raw semantic candidates require explicit review decisions.")
                else:
                    _require(cell["status"] != "not_reviewed", "Only semantic conditions require agent review.")
                duplicate = condition["kind"] == "has_near_duplicate"
                expected_source = ("embedding" if condition["kind"] == "semantic" else
                                   "photo" if duplicate and condition["metric"] == "exact" else "feature")
                for source in cell["sources"]:
                    _require(source["kind"] == expected_source
                             and (duplicate or source["photo_id"] == row["photo_id"])
                             and (expected_source == "photo" or source["profile_id"] == condition["profile_id"]),
                             "Condition evidence refers to an unrelated source type, photo or profile.")
                if condition["kind"] != "semantic" and cell["status"] in ("matched", "not_matched"):
                    own = [source for source in cell["sources"] if source["photo_id"] == row["photo_id"]]
                    _require(len(own) == 1, "Evaluated structured conditions need their own saved source.")
                    if duplicate and cell["status"] == "matched":
                        evidence = cell["evidence"]
                        peer_id = evidence.get("peer_id")
                        peers = [source for source in cell["sources"] if source["photo_id"] == peer_id]
                        _require(peer_id in candidate_ids and peer_id != row["photo_id"]
                                 and len(cell["sources"]) == 2 and len(peers) == 1
                                 and type(evidence.get("peer_count")) is int and evidence["peer_count"] >= 1
                                 and type(evidence.get("best_distance")) is int
                                 and 0 <= evidence["best_distance"] <= condition["max_distance"],
                                 "A duplicate match needs an in-scope peer and its saved witness.")
                        if condition["metric"] == "exact":
                            _require(own[0]["content_version"] == peers[0]["content_version"] == row["content_version"]
                                     and own[0]["ingest_state"] == peers[0]["ingest_state"] == "available",
                                     "Exact duplicate witnesses must identify the same available content.")
                    else:
                        _require(len(cell["sources"]) == 1, "Unexpected sources on a structured result.")
                if condition["kind"] == "semantic" and cell["status"] == "not_reviewed":
                    _require(type(cell["raw_score"]) in (int, float) and -1 <= cell["raw_score"] <= 1
                             and len(cell["sources"]) == 1 and cell["sources"][0]["kind"] == "embedding"
                             and cell["sources"][0]["photo_id"] == row["photo_id"]
                             and cell["sources"][0]["profile_id"] == condition["profile_id"]
                             and set(cell["evidence"]) == {"candidate_rank", "score_gap_from_best", "score_gap_to_next"},
                             "Semantic review must contain numerical embedding evidence only.")
                    evidence = cell["evidence"]
                    _require(type(evidence["candidate_rank"]) is int and evidence["candidate_rank"] > 0
                             and type(evidence["score_gap_from_best"]) in (int, float)
                             and 0 <= evidence["score_gap_from_best"] <= 2
                             and (evidence["score_gap_to_next"] is None or
                                  type(evidence["score_gap_to_next"]) in (int, float)
                                  and 0 <= evidence["score_gap_to_next"] <= 2), "Invalid semantic ranks or gaps.")
        coverage = snapshot["coverage"]
        _require(isinstance(coverage, dict) and set(coverage) == set(conditions), "Missing per-condition coverage.")
        for counts in coverage.values():
            _require(isinstance(counts, dict) and set(counts) <= set(STATES)
                     and all(type(value) is int and value >= 0 for value in counts.values())
                     and sum(counts.values()) == snapshot["scope_total"], "Invalid condition coverage counts.")
        retrieval = snapshot["retrieval"]
        semantic_ids = {key for key, value in conditions.items() if value["kind"] == "semantic"}
        expected_calls = (sum(len(plan["prompts"]) for plan in snapshot["query_encodings"].values())
                          if "query_encodings" in snapshot else len(semantic_ids))
        _require(snapshot["query_model_calls"] <= expected_calls, "Too many query model calls for the condition list.")
        _require(isinstance(retrieval, dict) and set(retrieval) == {
            "semantic_retrieval_limited", "unretrieved_photo_count", "semantic_eligible_counts"}
            and type(retrieval["semantic_retrieval_limited"]) is bool
            and type(retrieval["unretrieved_photo_count"]) is int
            and retrieval["unretrieved_photo_count"] == snapshot["scope_total"] - snapshot["candidate_count"]
            and isinstance(retrieval["semantic_eligible_counts"], dict)
            and set(retrieval["semantic_eligible_counts"]) == semantic_ids, "Invalid retrieval coverage.")
        limited = False
        for cid, count in retrieval["semantic_eligible_counts"].items():
            reviewed_count = sum(row["conditions"][cid]["status"] == "not_reviewed" for row in snapshot["candidates"])
            _require(type(count) is int and count == coverage[cid].get("not_reviewed", 0)
                     and count >= reviewed_count, "Invalid eligible semantic count.")
            limited |= count > reviewed_count
            ordered = _review_rows(snapshot, cid)
            previous_rank = 0
            for index, row in enumerate(ordered):
                cell = row["conditions"][cid]
                evidence = cell["evidence"]
                rank = evidence["candidate_rank"]
                _require(previous_rank < rank <= count and (index != 0 or rank == 1)
                         and math.isclose(evidence["score_gap_from_best"],
                                          ordered[0]["conditions"][cid]["raw_score"] - cell["raw_score"], abs_tol=1e-12),
                         "Semantic ranking or best-score gap is inconsistent.")
                _require((evidence["score_gap_to_next"] is None) == (rank == count),
                         "Only the last scoped embedding candidate has no next-score gap.")
                if index + 1 < len(ordered):
                    following = ordered[index + 1]["conditions"][cid]
                    if following["evidence"]["candidate_rank"] == rank + 1:
                        _require(math.isclose(evidence["score_gap_to_next"], cell["raw_score"] - following["raw_score"],
                                              abs_tol=1e-12), "Consecutive candidate score gaps disagree.")
                previous_rank = rank
        _require(limited == retrieval["semantic_retrieval_limited"]
                 and not (limited and canonical["semantic_candidates"] == "all"), "Candidate retrieval limits are inconsistent.")
        if snapshot["stage"] == "awaiting_semantic_decisions":
            _require(snapshot["results"] == [] and snapshot["normalization"] == {}
                     and snapshot["evaluated_coverage"] == {}
                     and snapshot["decisions"] is None and snapshot["ranking_version"] is None
                     and snapshot["source_digest"] is None and snapshot["finalized_at"] is None,
                     "Unfinalized snapshots cannot claim ranked results.")
        else:
            _require(_text(snapshot["finalized_at"]) and _text(snapshot["source_digest"]), "Final snapshot lacks provenance.")
            original = deepcopy(snapshot)
            original.update(stage="awaiting_semantic_decisions", results=[], normalization={}, decisions=None,
                            ranking_version=None, finalized_at=None, source_digest=None, evaluated_coverage={})
            _require(_seal(original)["digest"] == snapshot["source_digest"], "Final snapshot source digest mismatch.")
            resolved = _resolve_decisions(snapshot, snapshot["decisions"])
            _require(snapshot["evaluated_coverage"] == _evaluated_coverage(resolved, canonical["conditions"]),
                     "Final condition coverage does not match decisions.")
            ranked = snapshot["results"]
            validate_ranking(ranked, canonical["conditions"], snapshot["normalization"],
                             ranking_version=snapshot["ranking_version"], random_seed=snapshot["random_seed"])
            expected = {row["photo_id"]: row for row in resolved
                        if any(cell["status"] == "matched" for cell in row["conditions"].values())}
            _require(len(ranked) == len(expected) and {row["photo_id"] for row in ranked} == set(expected),
                     "Final results do not match the OR decisions.")
            for row in ranked:
                _require(set(row) == _ROW_FIELDS | _RANK_FIELDS, "Unexpected final result fields.")
                base = deepcopy(row)
                for key in _RANK_FIELDS:
                    base.pop(key)
                for cell in base["conditions"].values():
                    cell.pop("normalized_score", None)
                _require(base == expected[row["photo_id"]], "Final results changed captured condition evidence.")
    except (TypeError, KeyError, ValueError, OverflowError) as exc:
        raise PhotographyError("QUERY_SNAPSHOT_INVALID", "Malformed condition-search snapshot.") from exc
    return snapshot


def _validate_current(rows, store):
    from .feature_index import inspect_feature
    from .image_embedding import inspect_embedding

    checked = set()
    for row in rows:
        try:
            photo = store.photo(row["photo_id"])
            if photo["content_version"] != row["content_version"] or photo["ingest_state"] != "available":
                raise PhotographyError("QUERY_SNAPSHOT_STALE", "A candidate changed after retrieval.")
            for cell in row["conditions"].values():
                for source in cell["sources"]:
                    key = fingerprint(source)
                    if key in checked:
                        continue
                    checked.add(key)
                    current_photo = store.photo(source["photo_id"])
                    if source["kind"] == "photo":
                        valid = all(current_photo[field] == source[field] for field in ("content_version", "ingest_state"))
                    elif source["kind"] == "feature":
                        entry, record = inspect_feature(source["photo_id"], store.feature_profile(source["profile_id"]), store=store)
                        valid = entry["status"] == "ready" and all(record[field] == source[field] for field in
                                                                  ("result_id", "profile_id", "input_fingerprint", "payload_hash"))
                    else:
                        entry, record, _ = inspect_embedding(current_photo, store, store.embedding_profile(source["profile_id"]))
                        valid = entry["status"] == "ready" and all(record[field] == source[field] for field in
                            ("result_id", "profile_id", "content_version", "thumbnail_profile", "input_image_hash", "vector_hash"))
                    if not valid:
                        raise PhotographyError("QUERY_SNAPSHOT_STALE", "A result or dependency changed after retrieval.",
                                               details={"photo_id": source["photo_id"]})
        except PhotographyError as exc:
            if exc.code == "QUERY_SNAPSHOT_STALE":
                raise
            raise PhotographyError("QUERY_SNAPSHOT_STALE", "A captured query input is no longer available.",
                                   details={"photo_id": row["photo_id"], "cause": exc.code}) from exc


def semantic_evidence(snapshot, condition_id, page=0, *, store):
    with store.read_snapshot():
        _validate_snapshot(snapshot, store)
        if snapshot["stage"] != "awaiting_semantic_decisions":
            raise PhotographyError("QUERY_STAGE_INVALID", "This snapshot no longer needs semantic review.")
        condition = next((c for c in snapshot["query"]["conditions"] if c["id"] == condition_id), None)
        if condition is None or condition["kind"] != "semantic":
            raise PhotographyError("INVALID_ARGUMENT", "Choose an existing semantic condition ID.")
        return _page_evidence(snapshot, condition, page)


def _finalize_body(snapshot, decisions):
    resolved = _resolve_decisions(snapshot, decisions)
    ranking = rank_results(resolved, snapshot["query"]["conditions"], snapshot["random_seed"])
    result = deepcopy(snapshot)
    result.update(**ranking, stage="finalized", decisions=deepcopy(decisions),
                  finalized_at=datetime.now(timezone.utc).isoformat(), source_digest=snapshot["digest"],
                  evaluated_coverage=_evaluated_coverage(resolved, snapshot["query"]["conditions"]))
    return _seal(result)


def _evaluated_coverage(rows, conditions):
    return {condition["id"]: dict(Counter(row["conditions"][condition["id"]]["status"] for row in rows))
            for condition in conditions}


def finalize_query(snapshot, decisions, *, store):
    with store.read_snapshot():
        _validate_snapshot(snapshot, store)
        if snapshot["stage"] != "awaiting_semantic_decisions":
            raise PhotographyError("QUERY_STAGE_INVALID", "Finalize an original candidate snapshot.")
        _validate_current(snapshot["candidates"], store)
        return _finalize_body(snapshot, decisions)


def summary(snapshot, *, model_calls=0):
    semantic = []
    for condition in snapshot["query"]["conditions"]:
        if condition["kind"] == "semantic":
            count = sum(row["conditions"][condition["id"]]["status"] == "not_reviewed" for row in snapshot["candidates"])
            size = snapshot["query"]["review_page_size"]
            semantic.append({"condition_id": condition["id"], "query": condition["query"],
                             "profile_id": condition["profile_id"], "page_count": (count + size - 1) // size})
            if "query_encodings" in snapshot:
                semantic[-1]["query_encoding"] = deepcopy(snapshot["query_encodings"][condition["id"]])
    return {"schema": "condition-query-summary-v1", "album": snapshot["album"],
            "snapshot_id": snapshot["snapshot_id"], "stage": snapshot["stage"],
            "scope_total": snapshot["scope_total"], "candidate_count": snapshot["candidate_count"],
            "result_count": len(snapshot["results"]) if snapshot["stage"] == "finalized" else None,
            "retrieval": deepcopy(snapshot["retrieval"]), "aliases": deepcopy(snapshot["aliases"]),
            "semantic_conditions": semantic,
            "model_calls": model_calls, "image_model_calls": 0,
            "review_instruction": "Use query-evidence for semantic decisions; do not inspect the private candidate snapshot."}


def _cursor(snapshot, offset):
    identity = {"snapshot_id": snapshot["snapshot_id"], "digest": snapshot["digest"], "offset": offset}
    return f"{offset}:{fingerprint(identity)}"


def show_results(snapshot, *, store, limit=100, after=""):
    if type(limit) is not int or not 1 <= limit <= 1000 or not isinstance(after, str):
        raise PhotographyError("INVALID_ARGUMENT", "Use limit 1-1000 and a saved result cursor.")
    with store.read_snapshot():
        _validate_snapshot(snapshot, store)
        if snapshot["stage"] != "finalized":
            raise PhotographyError("QUERY_STAGE_INVALID", "Complete semantic review before showing ranked results.")
        offset = 0
        if after:
            try:
                offset = int(after.split(":", 1)[0])
            except ValueError as exc:
                raise PhotographyError("INVALID_ARGUMENT", "Malformed result cursor.") from exc
            if not 0 <= offset <= len(snapshot["results"]) or after != _cursor(snapshot, offset):
                raise PhotographyError("INVALID_ARGUMENT", "Cursor belongs to another result snapshot.")
        rows = snapshot["results"][offset:offset + limit]
        _validate_current(rows, store)
        return {"schema": "condition-search-page-v1", "snapshot_id": snapshot["snapshot_id"],
                "snapshot_digest": snapshot["digest"], "album": store.album(), "scope": deepcopy(snapshot["scope"]),
                "conditions": deepcopy(snapshot["query"]["conditions"]), "aliases": deepcopy(snapshot["aliases"]),
                "query_encodings": deepcopy(snapshot.get("query_encodings", {})),
                "coverage": deepcopy(snapshot["coverage"]),
                "evaluated_coverage": deepcopy(snapshot["evaluated_coverage"]),
                "retrieval": deepcopy(snapshot["retrieval"]), "scope_total": snapshot["scope_total"],
                "candidate_count": snapshot["candidate_count"], "total": len(snapshot["results"]),
                "ranking_version": snapshot["ranking_version"], "random_seed": snapshot["random_seed"],
                "results": deepcopy(rows), "created_at": snapshot["created_at"],
                "next_cursor": _cursor(snapshot, offset + limit) if offset + limit < len(snapshot["results"]) else None,
                "model_calls": 0, "image_model_calls": 0, "original_verification": "not_checked"}


def select_results(snapshot, photo_ids, *, store):
    if not isinstance(photo_ids, list) or any(not _text(pid) for pid in photo_ids):
        raise PhotographyError("INVALID_ARGUMENT", "Select explicit result photo IDs; [] means no membership changes.")
    with store.read_snapshot():
        _validate_snapshot(snapshot, store)
        if snapshot["stage"] != "finalized":
            raise PhotographyError("QUERY_STAGE_INVALID", "Only finalized query results may be added to a folder.")
        ids = set(photo_ids)
        offered = {row["photo_id"] for row in snapshot["results"]}
        if not ids <= offered:
            raise PhotographyError("INVALID_ARGUMENT", "Only IDs from this finalized query may be selected.")
        rows = [row for row in snapshot["results"] if row["photo_id"] in ids]
        _validate_current(rows, store)
        return {"snapshot_id": snapshot["snapshot_id"], "snapshot_digest": snapshot["digest"],
                "photo_ids": [row["photo_id"] for row in rows], "selected_count": len(rows),
                "result_count": len(snapshot["results"]), "scope": deepcopy(snapshot["scope"]),
                "method": "explicit_query_result_ids", "model_calls": 0, "image_model_calls": 0}


def _exact_pair_page(rows, values, offset, limit):
    """Skip whole exact-content groups while retaining global photo-ID pair order."""
    groups, positions = {}, {}
    for row in rows:
        photo_id = row["photo_id"]
        group = groups.setdefault(values[photo_id], [])
        positions[photo_id] = len(group)
        group.append(photo_id)
    total = sum(len(group) * (len(group) - 1) // 2 for group in groups.values())
    pairs = []
    remaining = offset
    for row in rows:
        left = row["photo_id"]
        group = groups[values[left]]
        start = positions[left] + 1
        count = len(group) - start
        if remaining >= count:
            remaining -= count
            continue
        start += remaining
        remaining = 0
        for right in group[start:start + limit - len(pairs)]:
            pairs.append({"photo_id_a": left, "photo_id_b": right, "distance": 0, "metric": "exact"})
        if len(pairs) == limit:
            break
    return total, pairs


def duplicate_pairs(snapshot, condition_id, *, store, limit=100, after=""):
    """Page actual pairs for a frozen duplicate condition, not transitive similarity groups."""
    if type(limit) is not int or not 1 <= limit <= 1000 or not isinstance(after, str):
        raise PhotographyError("INVALID_ARGUMENT", "Use limit 1-1000 and a saved pair cursor.")
    with store.read_snapshot():
        _validate_snapshot(snapshot, store)
        if snapshot["stage"] != "finalized":
            raise PhotographyError("QUERY_STAGE_INVALID", "Pair views require a finalized query.")
        condition = next((c for c in snapshot["query"]["conditions"] if c["id"] == condition_id), None)
        if condition is None or condition["kind"] != "has_near_duplicate":
            raise PhotographyError("INVALID_ARGUMENT", "Choose an existing duplicate condition.")
        rows = sorted((row for row in snapshot["results"] if row["conditions"][condition_id]["status"] == "matched"),
                      key=lambda row: row["photo_id"])
        _validate_current(rows, store)

        def cursor(offset):
            return f"{offset}:" + fingerprint({"snapshot_digest": snapshot["digest"],
                                               "condition_id": condition_id, "pair_offset": offset})

        offset = 0
        if after:
            try:
                offset = int(after.split(":", 1)[0])
            except ValueError as exc:
                raise PhotographyError("INVALID_ARGUMENT", "Malformed pair cursor.") from exc
            if offset < 0 or cursor(offset) != after:
                raise PhotographyError("INVALID_ARGUMENT", "Cursor belongs to another query or duplicate condition.")
        values = {}
        for row in rows:
            if condition["metric"] == "exact":
                values[row["photo_id"]] = row["content_version"]
            else:
                source = next((ref for ref in row["conditions"][condition_id]["sources"]
                               if ref["photo_id"] == row["photo_id"]), None)
                _require(source is not None, "A duplicate result lacks its own fingerprint source.")
                record = store.feature_result(source["result_id"])
                values[row["photo_id"]] = int(record["payload"]["hash_hex"], 16)
        if condition["metric"] == "exact":
            total, pairs = _exact_pair_page(rows, values, offset, limit)
        else:
            total, pairs = 0, []
            for index, left in enumerate(rows):
                for right in rows[index + 1:]:
                    a, b = left["photo_id"], right["photo_id"]
                    distance = (values[a] ^ values[b]).bit_count()
                    if distance > condition["max_distance"]:
                        continue
                    if total >= offset and len(pairs) < limit:
                        pairs.append({"photo_id_a": a, "photo_id_b": b, "distance": distance, "metric": "hamming"})
                    total += 1
        if offset > total:
            raise PhotographyError("INVALID_ARGUMENT", "Pair cursor is outside this snapshot.")
        return {"schema": "condition-duplicate-pairs-v1", "snapshot_id": snapshot["snapshot_id"],
                "snapshot_digest": snapshot["digest"], "album": store.album(), "scope": deepcopy(snapshot["scope"]),
                "condition": deepcopy(condition), "input_coverage": deepcopy(snapshot["coverage"][condition_id]),
                "pairs": pairs, "total": total,
                "next_cursor": cursor(offset + len(pairs)) if offset + len(pairs) < total else None,
                "grouping": "pairwise_only_not_transitive", "model_calls": 0, "image_model_calls": 0}
