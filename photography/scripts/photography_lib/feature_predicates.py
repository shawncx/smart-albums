"""Read-only predicates over current saved evidence; no images, inference or pair writes."""
from __future__ import annotations

import colorsys
from copy import deepcopy
import math

from . import feature_index
from .condition_queries import (
    PALETTE_RULE_VERSION, POSITION_RULE_VERSION, descriptor, normalize_query, validate_cell,
)
from .config import PhotographyError
from .feature_query_storage import ocr_matches
from .fingerprints import fingerprint


def _cell(condition, status, *, score=None, sources=(), evidence=None, reason=None):
    return validate_cell(condition, {
        "status": status, "raw_score": score, **descriptor(condition), "sources": list(sources),
        "evidence": {} if evidence is None else evidence, "reason": reason,
    })


def _feature_source(record):
    return {"kind": "feature", **{key: record[key] for key in
            ("photo_id", "profile_id", "result_id", "input_fingerprint", "payload_hash")}}


def _photo_source(photo):
    return {"kind": "photo", **{key: photo[key] for key in ("photo_id", "content_version", "ingest_state")}}


def _coverage(entry, record):
    status = entry["status"]
    if status == "ready":
        return None if record["payload"]["complete"] else ("unknown_invalid", "incomplete_result")
    if status == "missing":
        return "unknown_missing_index", "missing_index"
    if status == "stale":
        return "unknown_stale", "saved_input_changed"
    if status == "dependency_missing":
        dependency = (entry.get("error", {}).get("details") or {}).get("embedding_status")
        if dependency == "stale":
            return "unknown_stale", "stale_dependency"
        if dependency in ("invalid_input", "invalid_vector"):
            return "unknown_invalid", "invalid_dependency"
        return "unknown_missing_index", "missing_dependency"
    return "unknown_invalid", "invalid_saved_result"


def palette_color(rgb):
    """hsv-palette-v1: sRGB HSV; V<.20 black, S<.15 gray (V>=.85 white).

    Remaining hue intervals in degrees are [0,15) red, [15,45) orange,
    [45,75) yellow, [75,165) green, [165,195) cyan, [195,255) blue,
    [255,285) purple, [285,345) magenta, [345,360) red.
    """
    hue, saturation, value = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
    if value < .20:
        return "black"
    if saturation < .15:
        return "white" if value >= .85 else "gray"
    hue *= 360
    for upper, name in ((15, "red"), (45, "orange"), (75, "yellow"), (165, "green"),
                        (195, "cyan"), (255, "blue"), (285, "purple"), (345, "magenta")):
        if hue < upper:
            return name
    return "red"


def _third(value):
    """thirds-v1 intervals: [0,1/3), [1/3,2/3), [2/3,1]."""
    return 0 if value < 1 / 3 else 1 if value < 2 / 3 else 2


def _evaluate(condition, record, matches):
    kind, payload = condition["kind"], record["payload"]
    source = _feature_source(record)
    score = None
    if kind == "object_count":
        count = sum(item["class_id"] == condition["class_id"] and item["score"] >= condition["score_threshold"]
                    for item in payload["objects"])
        value = condition["value"]
        matched = {"eq": count == value, "ge": count >= value, "le": count <= value}[condition["operator"]]
        evidence = {"count": count}
    elif kind == "ocr_contains":
        snippet = matches.get(record["result_id"])
        matched = snippet is not None
        evidence = {"snippet": snippet} if matched else {}
    elif kind == "color_fraction":
        fraction = min(1., max(0., math.fsum(
            item["fraction"] for item in payload["palette"] if palette_color(item["rgb"]) == condition["color"])))
        matched = fraction >= condition["minimum"]
        score = fraction
        evidence = {"fraction": fraction, "rule_version": PALETTE_RULE_VERSION}
    elif kind == "subject_position":
        features = payload["features"]
        if features["subject_index"] is None:
            return _cell(condition, "unknown_invalid", sources=[source], reason="no_primary_subject")
        x, y = features["center_x"], features["center_y"]
        horizontal, vertical = condition["horizontal"], condition["vertical"]
        matched = ((horizontal is None or horizontal == ("left", "center", "right")[_third(x)])
                   and (vertical is None or vertical == ("top", "middle", "bottom")[_third(y)]))
        evidence = {"center_x": x, "center_y": y, "rule_version": POSITION_RULE_VERSION}
    else:
        score = next(item["score"] for item in payload["scores"] if item["scene_id"] == condition["scene_id"])
        matched = score >= condition["minimum"]
        evidence = {"score": score}
    if condition["scoring"] == "exact":
        score = 1 if matched else None
    return _cell(condition, "matched" if matched else "not_matched",
                 score=score, sources=[source], evidence=evidence)


def _duplicates(condition, photos, inspect, profiles):
    ready, result = {}, {}
    exact = condition["metric"] == "exact"
    for photo in photos:
        pid = photo["photo_id"]
        if exact:
            if photo["ingest_state"] != "available":
                result[pid] = _cell(condition, "unknown_invalid", reason="invalid_saved_photo")
                continue
            ready[pid] = (photo["content_version"], _photo_source(photo))
        else:
            entry, record = inspect(pid, profiles[condition["profile_id"]])
            unknown = _coverage(entry, record)
            if unknown:
                result[pid] = _cell(condition, unknown[0], sources=[_feature_source(record)] if record else [],
                                    reason=unknown[1])
                continue
            ready[pid] = (int(record["payload"]["hash_hex"], 16), _feature_source(record))
    # Retain a count and best witness, never an N-by-N matrix or per-photo neighbor arrays.
    peers = {pid: {"best_distance": None, "peer_id": None, "peer_count": 0} for pid in ready}
    ids = sorted(ready)
    if exact:
        groups = {}
        for pid in ids:
            groups.setdefault(ready[pid][0], []).append(pid)
        for group in groups.values():
            if len(group) > 1:
                for pid in group:
                    peers[pid] = {"best_distance": 0, "peer_id": group[0] if pid != group[0] else group[1],
                                  "peer_count": len(group) - 1}
    else:
        for index, pid in enumerate(ids):
            for other_index in range(index + 1, len(ids)):
                other = ids[other_index]
                distance = (ready[pid][0] ^ ready[other][0]).bit_count()
                if distance > condition["max_distance"]:
                    continue
                for own, peer in ((pid, other), (other, pid)):
                    evidence = peers[own]
                    evidence["peer_count"] += 1
                    if evidence["peer_id"] is None or (distance, peer) < (evidence["best_distance"], evidence["peer_id"]):
                        evidence.update(best_distance=distance, peer_id=peer)
    unknown_status = next((status for status in ("unknown_invalid", "unknown_stale", "unknown_missing_index")
                           if any(cell["status"] == status for cell in result.values())), None)
    for pid, evidence in peers.items():
        sources = [ready[pid][1]]
        if evidence["peer_id"] is not None:
            sources.append(ready[evidence["peer_id"]][1])
            result[pid] = _cell(condition, "matched", score=1, sources=sources, evidence=evidence)
        elif len(ready) < len(photos):
            result[pid] = _cell(condition, unknown_status, sources=sources, evidence=evidence,
                                reason="incomplete_peer_coverage")
        else:
            result[pid] = _cell(condition, "not_matched", sources=sources, evidence=evidence)
    return result


def evaluate_conditions(conditions, photos, *, store):
    """Return every supplied photo/condition cell. The caller holds the scope's read snapshot."""
    if not isinstance(conditions, list) or any(not isinstance(item, dict) or item.get("kind") == "semantic"
                                               for item in conditions):
        raise PhotographyError("INVALID_ARGUMENT", "Saved-feature evaluation accepts non-semantic conditions only.")
    if not isinstance(photos, list) or any(not isinstance(photo, dict) for photo in photos):
        raise PhotographyError("INVALID_ARGUMENT", "Provide scoped saved photo records.")
    ids = [photo.get("photo_id") for photo in photos]
    if any(not isinstance(pid, str) or not pid for pid in ids) or len(ids) != len(set(ids)):
        raise PhotographyError("INVALID_ARGUMENT", "Scoped photo IDs must be unique nonblank strings.")
    matrix = {pid: {} for pid in ids}
    if not conditions:
        return matrix
    normalized = normalize_query({"conditions": conditions}, store=store)
    canonical, aliases = normalized["query"]["conditions"], normalized["aliases"]
    profiles = {profile_id: store.feature_profile(profile_id)
                for profile_id in {condition["profile_id"] for condition in canonical} if profile_id is not None}
    cache = {}

    def inspect(pid, profile):
        key = (pid, fingerprint(profile))
        if key not in cache:
            entry, record = feature_index.inspect_feature(pid, profile, store=store)
            if entry["status"] == "dependency_missing" and profile["component"] == "composition":
                source_id = profile["dependencies"]["objects"]
                if source_id not in profiles:
                    profiles[source_id] = store.feature_profile(source_id)
                source_entry, source_record = inspect(pid, profiles[source_id])
                unknown = _coverage(source_entry, source_record)
                if unknown and unknown[0] == "unknown_stale":
                    entry = {**entry, "status": "stale"}
                elif unknown and unknown[0] == "unknown_invalid":
                    entry = {**entry, "status": "invalid_result"}
            cache[key] = entry, record
        return cache[key]

    for condition in canonical:
        cid = condition["id"]
        if condition["kind"] == "has_near_duplicate":
            for pid, cell in _duplicates(condition, photos, inspect, profiles).items():
                matrix[pid][cid] = cell
            continue
        profile = profiles[condition["profile_id"]]
        inspected = {pid: inspect(pid, profile) for pid in ids}
        matches = {}
        if condition["kind"] == "ocr_contains":
            current_ids = [record["result_id"] for entry, record in inspected.values()
                           if _coverage(entry, record) is None]
            matches = ocr_matches(condition["text"], current_ids, store=store)
        for pid, (entry, record) in inspected.items():
            unknown = _coverage(entry, record)
            if unknown:
                matrix[pid][cid] = _cell(condition, unknown[0],
                                        sources=[_feature_source(record)] if record else [], reason=unknown[1])
            else:
                matrix[pid][cid] = _evaluate(condition, record, matches)
    for photo_cells in matrix.values():
        for original, canonical_id in aliases.items():
            if original != canonical_id:
                photo_cells[original] = deepcopy(photo_cells[canonical_id])
    return matrix
