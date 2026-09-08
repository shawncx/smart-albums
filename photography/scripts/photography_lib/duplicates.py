"""Read-only duplicate discovery and frozen, fully enumerable candidate groups."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import re
from uuid import UUID, uuid4

from .config import PhotographyError
from .fingerprints import fingerprint
from .virtual_folders import resolve_scope


SCHEMA = "duplicate-snapshot-v1"
GROUPING = "connected_candidates_not_equivalence"
ALGORITHM = "sha256-dhash64-v1"
HASH = re.compile(r"[0-9a-f]{64}\Z")
DHASH = re.compile(r"[0-9a-f]{16}\Z")
STATUSES = ("eligible", "missing", "stale", "invalid_input", "invalid_result",
            "dependency_missing", "incomplete_result", "not_configured", "not_requested")


def _require(ok, message, code="INVALID_ARGUMENT"):
    if not ok:
        raise PhotographyError(code, message)


def _hex(value, pattern=HASH):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _parameters(mode, profile_id, max_distance, store):
    from .feature_index import resolve_profile

    _require(mode in ("exact", "similar", "all"), "Choose duplicate mode exact, similar or all.")
    if mode == "exact":
        _require(profile_id is None and max_distance is None, "Exact mode uses neither a hash profile nor a distance.")
        profile, distance = None, None
    else:
        distance = 8 if max_distance is None else max_distance
        _require(type(distance) is int and 0 <= distance <= 64, "Use an integer Hamming distance from 0 to 64.")
        chosen = profile_id if profile_id is not None else store.default_feature_profile("perceptual_hash")
        profile = resolve_profile("perceptual_hash", store=store, profile_id=chosen) if chosen is not None else None
        if profile is not None:
            p = profile["parameters"]
            _require(p["algorithm"] == "dhash" and p["bits"] == 64,
                     "Duplicate discovery requires a dHash64 profile.", "FEATURE_PROFILE_MISMATCH")
    return {"mode": mode, "profile_id": fingerprint(profile) if profile else None,
            "max_distance": distance, "algorithm": ALGORITHM, "grouping": GROUPING}, profile


def _scope(store, all_photos, folder_ids, folder_match, photo_ids):
    _require(type(all_photos) is bool and sum((all_photos, folder_ids is not None, photo_ids is not None)) == 1,
             "Select exactly --all, --folder-id or --ids-file.")
    _require(folder_ids is not None or folder_match is None, "Folder match requires folder IDs.")
    if photo_ids is not None:
        _require(isinstance(photo_ids, list) and all(isinstance(pid, str) and pid.strip() for pid in photo_ids),
                 "Use a JSON array of photo IDs; [] is an empty selection.")
        ids = sorted(set(photo_ids))
        return {"kind": "photo_ids", "photo_ids": ids}, [store.photo(pid) for pid in ids]
    if folder_ids is not None:
        _require(isinstance(folder_ids, list) and bool(folder_ids), "Select at least one folder ID.")
    scope, photos = resolve_scope(store=store, folder_ids=folder_ids, folder_match=folder_match)
    return scope, sorted(photos, key=lambda row: row["photo_id"])


def _input(photo, parameters, profile, store):
    from .feature_index import inspect_feature

    row = {key: deepcopy(photo.get(key)) for key in (
        "photo_id", "content_version", "thumbnail_profile", "original_absolute_path", "original_relative_path",
        "size_bytes", "ingest_state",
    )}
    metadata = photo.get("metadata", {})
    row["metadata"] = {key: metadata.get(key) if type(metadata.get(key)) is int and metadata[key] > 0 else None
                       for key in ("width", "height", "display_width", "display_height")}
    captured = metadata.get("exif", {}).get("datetime_original")
    row["metadata"]["datetime_original"] = str(captured)[:100] if captured is not None else None
    row.update(input_image_hash=None, hash=None, exact_status="not_requested", similar_status="not_requested")
    try:
        thumbnail = store.thumbnail(photo["photo_id"], include_data=False)
        if (thumbnail["content_version"], thumbnail["profile"]) == (row["content_version"], row["thumbnail_profile"]):
            row["input_image_hash"] = thumbnail["image_hash"]
    except PhotographyError as exc:
        if exc.code != "INVALID_PREVIEW":
            raise
    valid = photo["ingest_state"] == "available" and _hex(photo["content_version"])
    if parameters["mode"] != "similar":
        row["exact_status"] = "eligible" if valid else "invalid_input"
    if parameters["mode"] != "exact":
        if not valid:
            row["similar_status"] = "invalid_input"
        elif profile is None:
            row["similar_status"] = "not_configured"
        else:
            entry, record = inspect_feature(photo["photo_id"], profile, store=store)
            state = entry["status"]
            if state == "ready":
                if record is None or not record["payload"]["complete"]:
                    state = "incomplete_result"
                else:
                    state = "eligible"
                    row["hash"] = {key: record[key] for key in
                                   ("profile_id", "result_id", "input_fingerprint", "payload_hash")}
                    row["hash"]["hash_hex"] = record["payload"]["hash_hex"]
            row["similar_status"] = state
    return row


def _coverage(inputs, mode):
    coverage = {}
    for branch in ("exact", "similar"):
        counts = dict(Counter(row[branch + "_status"] for row in inputs))
        requested = mode == "all" or branch == mode
        eligible = counts.get("eligible", 0)
        coverage[branch] = {"requested": requested, "total": len(inputs), "eligible": eligible,
                            "unchecked": len(inputs) - eligible if requested else 0,
                            "counts": counts, "complete": not requested or eligible == len(inputs)}
    return coverage


def _build_groups(inputs, parameters):
    """Linear storage; exact buckets are implicit cliques, hash edges are streamed."""
    parent = {row["photo_id"]: row["photo_id"] for row in inputs}
    sizes = dict.fromkeys(parent, 1)
    matches = {pid: {"exact_peer_count": 0, "similar_peer_count": 0, "best_match": None,
                     "min_hamming_distance": None} for pid in parent}

    def root(pid):
        while parent[pid] != pid:
            parent[pid] = parent[parent[pid]]
            pid = parent[pid]
        return pid

    def union(a, b):
        a, b = root(a), root(b)
        if a != b:
            if sizes[a] < sizes[b]:
                a, b = b, a
            parent[b] = a
            sizes[a] += sizes[b]

    buckets = defaultdict(list)
    for row in inputs:
        if row["exact_status"] == "eligible":
            buckets[row["content_version"]].append(row["photo_id"])
    exact_groups = [members for members in buckets.values() if len(members) > 1]
    for members in exact_groups:
        for pid in members:
            union(members[0], pid)
            matches[pid].update(exact_peer_count=len(members) - 1,
                                best_match={"photo_id": members[0] if pid != members[0] else members[1],
                                            "metric": "exact", "distance": 0})
    ready = [(row["photo_id"], int(row["hash"]["hash_hex"], 16),
              row["content_version"] if row["exact_status"] == "eligible" else None)
             for row in inputs if row["similar_status"] == "eligible"]
    threshold = parameters["max_distance"]
    for i, (a, value, content) in enumerate(ready):
        for j in range(i + 1, len(ready)):
            b, other_value, other_content = ready[j]
            if content is not None and content == other_content:
                continue
            distance = (value ^ other_value).bit_count()
            if distance > threshold:
                continue
            union(a, b)
            for own, peer in ((a, b), (b, a)):
                evidence = matches[own]
                evidence["similar_peer_count"] += 1
                previous = evidence["min_hamming_distance"]
                evidence["min_hamming_distance"] = distance if previous is None else min(previous, distance)
                best = evidence["best_match"]
                if best is None or (best["metric"] != "exact" and (distance, peer) < (best["distance"], best["photo_id"])):
                    evidence["best_match"] = {"photo_id": peer, "metric": "hamming", "distance": distance}
    components, subgroups = defaultdict(list), defaultdict(list)
    for pid in parent:
        if matches[pid]["best_match"] is not None:
            components[root(pid)].append(pid)
    for members in exact_groups:
        subgroups[root(members[0])].append(members)
    by_id = {row["photo_id"]: row for row in inputs}
    groups = []
    for representative, members in components.items():
        exact = sum(matches[pid]["exact_peer_count"] for pid in members) // 2
        similar = sum(matches[pid]["similar_peer_count"] for pid in members) // 2
        minimum = min((matches[pid]["min_hamming_distance"] for pid in members
                       if matches[pid]["min_hamming_distance"] is not None), default=None)
        identity = [{key: by_id[pid][key] for key in ("photo_id", "content_version", "hash")} for pid in members]
        groups.append({"group_id": "duplicate_group_" + fingerprint({"parameters": parameters, "inputs": identity}),
                       "kind": "mixed" if exact and similar else "exact" if exact else "similar",
                       "member_ids": members, "member_count": len(members),
                       "exact_subgroups": sorted(subgroups[representative]),
                       "pair_counts": {"exact": exact, "similar": similar, "total": exact + similar},
                       "min_hamming_distance": minimum, "grouping": GROUPING})
    groups.sort(key=lambda group: (group["kind"] != "exact",
                                  group["min_hamming_distance"] if group["min_hamming_distance"] is not None else -1,
                                  -group["member_count"], group["member_ids"][0]))
    for number, group in enumerate(groups, 1):
        group["number"] = number
    return groups, matches


def _summary(groups):
    return {"group_count": len(groups), "matched_photo_count": sum(g["member_count"] for g in groups),
            "pair_count": sum(g["pair_counts"]["total"] for g in groups),
            "exact_pair_count": sum(g["pair_counts"]["exact"] for g in groups),
            "similar_pair_count": sum(g["pair_counts"]["similar"] for g in groups),
            "group_counts": dict(Counter(g["kind"] for g in groups))}


def scan(*, store, all_photos=False, folder_ids=None, folder_match=None, photo_ids=None,
         mode="all", profile_id=None, max_distance=None):
    with store.read_snapshot():
        parameters, profile = _parameters(mode, profile_id, max_distance, store)
        scope, photos = _scope(store, all_photos, folder_ids, folder_match, photo_ids)
        inputs = [_input(photo, parameters, profile, store) for photo in photos]
        album = deepcopy(store.album())
    groups, matches = _build_groups(inputs, parameters)
    for row in inputs:
        row["match"] = matches[row["photo_id"]]
    coverage = _coverage(inputs, mode)
    complete = all(branch["complete"] for branch in coverage.values())
    snapshot = {"schema": SCHEMA, "snapshot_id": "duplicates_" + uuid4().hex,
                "created_at": datetime.now(timezone.utc).isoformat(), "album": album, "scope": scope,
                "parameters": parameters, "inputs": inputs, "groups": groups, "coverage": coverage,
                "summary": _summary(groups), "scan_finished": True, "complete": complete,
                "status": "completed" if complete else "partial", "original_verification": "not_checked",
                "model_calls": 0, "image_model_calls": 0}
    snapshot["digest"] = fingerprint(snapshot)
    return snapshot


def _relation(left, right, parameters):
    if (left["exact_status"] == right["exact_status"] == "eligible"
            and left["content_version"] == right["content_version"]):
        return {"metric": "exact", "distance": 0}
    if left["similar_status"] == right["similar_status"] == "eligible":
        distance = (int(left["hash"]["hash_hex"], 16) ^ int(right["hash"]["hash_hex"], 16)).bit_count()
        if distance <= parameters["max_distance"]:
            return {"metric": "hamming", "distance": distance}
    return None


def validate(snapshot, *, store):
    """Validate frozen data in O(N), without querying current photos or features."""
    code = "DUPLICATE_SNAPSHOT_INVALID"
    try:
        def require(ok):
            _require(ok, "Expected an intact duplicate-snapshot-v1 snapshot.", code)

        require(isinstance(snapshot, dict) and snapshot["schema"] == SCHEMA)
        require(_hex(snapshot["digest"]) and snapshot["digest"] == fingerprint(
            {key: value for key, value in snapshot.items() if key != "digest"}))
        UUID(snapshot["album"]["id"])
        _require(snapshot["album"]["id"] == store.album()["id"], "This duplicate snapshot belongs to another album.",
                 "ALBUM_MISMATCH")
        require(isinstance(snapshot["snapshot_id"], str) and snapshot["snapshot_id"].startswith("duplicates_"))
        parameters = snapshot["parameters"]
        mode, threshold = parameters["mode"], parameters["max_distance"]
        require(mode in ("exact", "similar", "all") and parameters["algorithm"] == ALGORITHM
                and parameters["grouping"] == GROUPING)
        require(threshold is None if mode == "exact" else type(threshold) is int and 0 <= threshold <= 64)
        require(parameters["profile_id"] is None or _hex(parameters["profile_id"]))
        require(mode != "exact" or parameters["profile_id"] is None)
        rows = snapshot["inputs"]
        require(isinstance(rows, list))
        ids = [row["photo_id"] for row in rows]
        require(all(isinstance(pid, str) and pid.strip() for pid in ids) and ids == sorted(set(ids)))
        by_id = dict(zip(ids, rows))
        scope = snapshot["scope"]
        require(scope["kind"] in ("album", "photo_ids", "virtual_folders"))
        if scope["kind"] == "photo_ids":
            require(scope["photo_ids"] == ids)
        if scope["kind"] == "virtual_folders":
            require(scope["match"] in ("union", "intersection") and bool(scope["folders"]))
            require(all(isinstance(f["folder_id"], str) and isinstance(f["name"], str) for f in scope["folders"]))
        for row in rows:
            require(isinstance(row["original_absolute_path"], str) and bool(row["original_absolute_path"]))
            require(row["original_relative_path"] is None or isinstance(row["original_relative_path"], str))
            require(isinstance(row["thumbnail_profile"], str) and isinstance(row["metadata"], dict))
            require(row["input_image_hash"] is None or _hex(row["input_image_hash"]))
            require(type(row["size_bytes"]) is int and row["size_bytes"] >= 0)
            metadata = row["metadata"]
            require(all(metadata[key] is None or type(metadata[key]) is int and metadata[key] > 0
                        for key in ("width", "height", "display_width", "display_height")))
            require(metadata["datetime_original"] is None or isinstance(metadata["datetime_original"], str)
                    and len(metadata["datetime_original"]) <= 100)
            for branch in ("exact", "similar"):
                state = row[branch + "_status"]
                require(state in STATUSES and (state != "not_requested") == (mode in (branch, "all")))
                if state == "eligible":
                    require(row["ingest_state"] == "available" and _hex(row["content_version"]))
            if row["similar_status"] == "eligible":
                evidence = row["hash"]
                require(evidence["profile_id"] == parameters["profile_id"] and _hex(evidence["profile_id"]))
                require(_hex(evidence["hash_hex"], DHASH) and _hex(evidence["input_fingerprint"])
                        and _hex(evidence["payload_hash"]) and isinstance(evidence["result_id"], str))
            else:
                require(row["hash"] is None)
            match = row["match"]
            require(all(type(match[k]) is int and 0 <= match[k] < max(1, len(rows))
                        for k in ("exact_peer_count", "similar_peer_count")))
            best = match["best_match"]
            require((best is None) == (match["exact_peer_count"] + match["similar_peer_count"] == 0))
            if best is not None:
                require(best["photo_id"] in by_id and best["photo_id"] != row["photo_id"])
                relation = _relation(row, by_id[best["photo_id"]], parameters)
                require(relation is not None and relation == {key: best[key] for key in ("metric", "distance")})
            minimum = match["min_hamming_distance"]
            require((minimum is None) == (match["similar_peer_count"] == 0))
            if minimum is not None:
                require(type(minimum) is int and threshold is not None and 0 <= minimum <= threshold)
        seen, group_ids = set(), set()
        for number, group in enumerate(snapshot["groups"], 1):
            members = group["member_ids"]
            require(isinstance(members, list) and len(members) >= 2 and members == sorted(set(members)))
            require(set(members) <= by_id.keys() and not seen.intersection(members))
            seen.update(members)
            require(group["number"] == number and group["member_count"] == len(members) and group["grouping"] == GROUPING)
            require(isinstance(group["group_id"], str) and group["group_id"] not in group_ids)
            identity = [{key: by_id[pid][key] for key in ("photo_id", "content_version", "hash")} for pid in members]
            require(group["group_id"] == "duplicate_group_" + fingerprint({"parameters": parameters, "inputs": identity}))
            group_ids.add(group["group_id"])
            member_set = set(members)
            expected_subgroups = defaultdict(list)
            for pid in members:
                row, match = by_id[pid], by_id[pid]["match"]
                require(match["best_match"] is not None and match["best_match"]["photo_id"] in member_set)
                require(match["exact_peer_count"] + match["similar_peer_count"] < len(members))
                if row["exact_status"] == "eligible":
                    expected_subgroups[row["content_version"]].append(pid)
            require(group["exact_subgroups"] == sorted(m for m in expected_subgroups.values() if len(m) > 1))
            exact = sum(len(m) * (len(m) - 1) // 2 for m in group["exact_subgroups"])
            similar_degrees = sum(by_id[pid]["match"]["similar_peer_count"] for pid in members)
            require(similar_degrees % 2 == 0)
            similar = similar_degrees // 2
            require(sum(by_id[pid]["match"]["exact_peer_count"] for pid in members) == 2 * exact)
            require(group["pair_counts"] == {"exact": exact, "similar": similar, "total": exact + similar})
            require(group["kind"] == ("mixed" if exact and similar else "exact" if exact else "similar"))
            minimum = min((by_id[pid]["match"]["min_hamming_distance"] for pid in members
                           if by_id[pid]["match"]["min_hamming_distance"] is not None), default=None)
            require(group["min_hamming_distance"] == minimum)
        require(seen == {row["photo_id"] for row in rows if row["match"]["best_match"] is not None})
        require(snapshot["summary"] == _summary(snapshot["groups"]))
        coverage = _coverage(rows, mode)
        complete = all(branch["complete"] for branch in coverage.values())
        require(snapshot["coverage"] == coverage and snapshot["complete"] is complete and snapshot["scan_finished"] is True)
        require(snapshot["status"] == ("completed" if complete else "partial")
                and snapshot["original_verification"] == "not_checked"
                and snapshot["model_calls"] == snapshot["image_model_calls"] == 0)
        return by_id
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError) as exc:
        raise PhotographyError(code, "Malformed duplicate snapshot.") from exc


def summary(snapshot):
    return {"schema": "duplicate-summary-v1", **{key: deepcopy(snapshot[key]) for key in (
        "snapshot_id", "album", "scope", "parameters", "coverage", "summary", "status", "scan_finished",
        "complete", "original_verification", "model_calls", "image_model_calls",
    )}, "historical": True}


def _page_limit(value):
    _require(value == "all" or type(value) is int and value > 0, "Use a positive page size or all.")
    return value


def _cursor(snapshot, view, group_id, position):
    return str(position) + ":" + fingerprint({"snapshot_id": snapshot["snapshot_id"], "digest": snapshot["digest"],
                                             "view": view, "group_id": group_id, "position": position})


def _position(snapshot, view, group_id, after, maximum):
    _require(isinstance(after, str), "Use the returned page cursor.")
    if not after:
        return 0
    try:
        position = int(after.split(":", 1)[0])
    except ValueError:
        raise PhotographyError("INVALID_ARGUMENT", "Malformed duplicate cursor.") from None
    _require(0 <= position <= maximum and after == _cursor(snapshot, view, group_id, position),
             "Cursor belongs to another snapshot, view, group or position.")
    return position


def _group(snapshot, group_id):
    _require(isinstance(group_id, str), "Choose a group ID from this snapshot.")
    group = next((group for group in snapshot["groups"] if group["group_id"] == group_id), None)
    _require(group is not None, "Group does not belong to this snapshot.")
    return group


def page(snapshot, *, store, view="groups", group_id=None, limit=50, after=""):
    by_id = validate(snapshot, store=store)
    _page_limit(limit)
    _require(view in ("groups", "group", "pairs"), "Unknown duplicate view.")
    _require(view != "groups" or group_id is None, "The groups view does not take a group ID.")
    result = {"schema": "duplicate-page-v1", "snapshot_id": snapshot["snapshot_id"], "album": deepcopy(snapshot["album"]),
              "view": view, "group_id": group_id, "historical": True, "complete": snapshot["complete"],
              "coverage": deepcopy(snapshot["coverage"]), "original_verification": "not_checked",
              "model_calls": 0, "image_model_calls": 0, "grouping": GROUPING}
    if view == "groups":
        # Keep a single large group from defeating group pagination.
        items = [{k: deepcopy(v) for k, v in g.items() if k not in ("member_ids", "exact_subgroups")}
                 for g in snapshot["groups"]]
    else:
        group = _group(snapshot, group_id)
        result["group"] = {k: deepcopy(v) for k, v in group.items() if k not in ("member_ids", "exact_subgroups")}
        items = [by_id[pid] for pid in group["member_ids"]]
    if view != "pairs":
        start = _position(snapshot, view, group_id, after, len(items))
        stop = len(items) if limit == "all" else min(len(items), start + limit)
        result.update(items=deepcopy(items[start:stop]), total=len(items), next_cursor=(
            _cursor(snapshot, view, group_id, stop) if stop < len(items) else None))
        return result
    # Flattened upper-triangle position allows continuation without revisiting earlier pairs.
    n = len(items)
    maximum = n * (n - 1) // 2
    start = _position(snapshot, view, group_id, after, maximum)
    position, remaining, i = start, start, 0
    while i < n - 1 and remaining >= n - i - 1:
        remaining -= n - i - 1
        i += 1
    j = i + 1 + remaining
    pairs, next_position = [], None
    while i < n - 1:
        while j < n:
            relation = _relation(items[i], items[j], snapshot["parameters"])
            if relation is not None:
                if limit != "all" and len(pairs) == limit:
                    next_position = position
                    break
                pairs.append({"photo_id_a": items[i]["photo_id"], "photo_id_b": items[j]["photo_id"], **relation})
            position += 1
            j += 1
        if next_position is not None:
            break
        i, j = i + 1, i + 2
    result.update(pairs=pairs, total=group["pair_counts"]["total"], next_cursor=(
        _cursor(snapshot, view, group_id, next_position) if next_position is not None else None))
    return result
