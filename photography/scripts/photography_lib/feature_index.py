"""Explicit, versioned execution of local feature components, independent of search."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .config import PhotographyError
from .feature_inputs import load_input, manifest_for, verify_manifest
from .feature_profiles import COMPONENTS, profile_identity, validate_payload
from .fingerprints import fingerprint
from .index_lock import execution_lock


STATES = ("pending", "running", "cached", "computed", "failed", "stale", "invalid_input",
          "input_unavailable", "dependency_missing", "cancelled")
COVERAGE = ("ready", "missing", "stale", "invalid_input", "invalid_result", "dependency_missing")
SKIPPABLE_ERRORS = {
    "FEATURE_INVALID_INPUT": "invalid_input", "INVALID_PREVIEW": "invalid_input",
    "FEATURE_DEPENDENCY_MISSING": "dependency_missing",
    "FEATURE_RESULT_INVALID": "invalid_result", "FEATURE_INPUT_CHANGED": "stale",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def resolve_profile(component, *, store, profile_id=None):
    if component not in COMPONENTS:
        raise PhotographyError("INVALID_ARGUMENT", "Unknown feature component.")
    chosen = profile_id if profile_id is not None else store.default_feature_profile(component)
    if chosen is None:
        raise PhotographyError("FEATURE_CONFIGURATION_REQUIRED", "Explicitly configure this component's profile first.")
    profile = store.feature_profile(chosen)
    if profile["component"] != component:
        raise PhotographyError("FEATURE_PROFILE_MISMATCH", "The selected profile belongs to another component.")
    return profile


def _ids(ids, store):
    if (not isinstance(ids, list) or not ids
            or any(not isinstance(pid, str) or not pid.strip() for pid in ids)):
        raise PhotographyError("NO_PHOTOS_SELECTED", "Select a nonempty array of real photo IDs.")
    selected = sorted(set(ids))
    for pid in selected:
        store.photo(pid)
    return selected


def inspect_feature(photo_id, profile, *, store):
    profile_id, _ = profile_identity(profile)
    entry = {"photo_id": photo_id, "profile_id": profile_id, "component": profile["component"],
             "original_verification": "not_checked"}
    try:
        manifest = manifest_for(photo_id, profile, store=store)
        key = fingerprint(manifest)
        entry.update(manifest=manifest, input_fingerprint=key)
        record = store.find_feature_result(photo_id, profile_id, key)
        if record is not None:
            entry.update(status="ready", result_id=record["result_id"],
                         complete=record["payload"]["complete"])
            return entry, record
        entry["status"] = "stale" if store.has_feature_results(photo_id, profile_id) else "missing"
    except PhotographyError as exc:
        if exc.code not in SKIPPABLE_ERRORS:
            raise
        entry.update(status=SKIPPABLE_ERRORS[exc.code], error=exc.to_dict())
    return entry, None


def status(*, store, profile, limit=100, after="", status_filter=None):
    store._limit(limit)
    if not isinstance(after, str):
        raise PhotographyError("INVALID_ARGUMENT", "Use a photo ID cursor.")
    if status_filter is not None and status_filter not in COVERAGE:
        raise PhotographyError("INVALID_ARGUMENT", "Unknown feature coverage status.")
    with store.read_snapshot():
        items = [inspect_feature(photo["photo_id"], profile, store=store)[0] for photo in store.photos()]
        counts = {key: sum(item["status"] == key for item in items) for key in COVERAGE}
        remaining = [item for item in items if item["photo_id"] > after
                     and (status_filter is None or item["status"] == status_filter)]
        page = remaining[:limit]
        return {"album": store.album(), "component": profile["component"], "profile_id": fingerprint(profile),
                "coverage": {"total": len(items), **counts}, "coverage_scope": "entire_album",
                "items": page, "next_cursor": page[-1]["photo_id"] if len(remaining) > limit else None,
                "model_calls": 0, "original_verification": "not_checked"}


def result_summary(record, *, details=False, limit=100, after=0):
    if type(limit) is not int or not 1 <= limit <= 1000 or type(after) is not int or after < 0:
        raise PhotographyError("INVALID_ARGUMENT", "Result paging needs limit 1-1000 and a nonnegative offset.")
    result = {key: deepcopy(record[key]) for key in (
        "result_id", "photo_id", "profile_id", "component", "content_version", "input_scope",
        "input_fingerprint", "input_manifest", "payload_hash", "created_at")}
    payload = record["payload"]
    result["complete"] = payload["complete"]
    list_key = {"ocr": "blocks", "objects": "objects", "scene": "scores", "color": "palette"}.get(record["component"])
    if list_key:
        result["detail_count"] = len(payload[list_key])
    if record["component"] == "ocr":
        result["text_length"] = len(payload["text"])
    if details:
        result["payload"] = deepcopy(payload)
        if list_key:
            result["payload"][list_key] = result["payload"][list_key][after:after + limit]
            result["next_cursor"] = after + limit if after + limit < len(payload[list_key]) else None
            if record["component"] == "ocr":
                # Full-document text is not needed to page through explicitly requested blocks.
                result["payload"].pop("text")
                result["payload"].pop("normalized_text")
    return result


def current_result(photo_id, *, store, profile, details=False, limit=100, after=0):
    with store.read_snapshot():
        entry, record = inspect_feature(photo_id, profile, store=store)
        return {"album": store.album(), **entry,
                "result": result_summary(record, details=details, limit=limit, after=after) if record else None,
                "model_calls": 0}


def _save_plan(items, profile, work_kind, *, store, persist, options=None):
    profile_id, _ = profile_identity(profile)
    plan = {"schema": "image-feature-plan-v1", "run_id": "feature_" + uuid4().hex,
            "work_kind": work_kind, "album_id": store.album()["id"],
            "component": profile["component"], "profile_id": profile_id, "profile": deepcopy(profile),
            "created_at": now(), "options": deepcopy(options or {}), "items": deepcopy(items),
            "counts": {"total": len(items), **{key: sum(item["action"] == key for item in items)
                                              for key in ("compute", "reuse", "skip")}}}
    plan["digest"] = fingerprint(plan)
    if persist:
        with store.transaction():
            store.put_feature_profile(profile)
            store.create_feature_run(plan)
    return {**plan, "status": "proposed" if persist else "dry_run", "album": store.album(), "model_calls": 0}


def _vision_provider(profile, config, python_path):
    from .feature_vision import VisionProvider

    return VisionProvider(profile, config=config, python_path=python_path)


def create_plan(ids, *, store, config, profile, provider=None, python_path=None, persist=True):
    if store.db.in_transaction:
        raise PhotographyError("FEATURE_TRANSACTION_ACTIVE", "Prepare feature plans outside existing transactions.")
    items = []
    with store.read_snapshot():
        for pid in _ids(ids, store):
            entry, record = inspect_feature(pid, profile, store=store)
            action = "reuse" if record else "compute" if entry["status"] in ("missing", "stale") else "skip"
            manifest = entry.get("manifest", {"content_version": store.photo(pid)["content_version"],
                                               "input_scope": profile["input_scope"]})
            items.append({"item_id": pid, "photo_id": pid, "manifest": manifest,
                          "input_fingerprint": fingerprint(manifest), "action": action,
                          "result_id": record["result_id"] if record else None,
                          "reason": entry["status"], "error": entry.get("error")})
    if any(item["action"] == "compute" for item in items) and profile["component"] in ("ocr", "objects"):
        if provider is not None:
            _check_provider(provider, profile)
            provider.check_ready()
        else:
            with _vision_provider(profile, config, python_path) as runtime:
                runtime.check_ready()
    return _save_plan(items, profile, "extract", store=store, persist=persist)


def _prototype_manifest(profile, store):
    embedding_id = profile["dependencies"]["image_embedding"]
    source = store.embedding_profile(embedding_id)
    return {"embedding_profile_id": embedding_id, "embedding_profile_hash": fingerprint(source),
            "catalog": deepcopy(profile["parameters"]["catalog"])}


def create_prototype_plan(*, store, config, profile, persist=True, encoder=None):
    if profile["component"] != "scene":
        raise PhotographyError("INVALID_ARGUMENT", "Prototype preparation requires a scene profile.")
    with store.read_snapshot():
        manifest = _prototype_manifest(profile, store)
        existing = store.scene_prototype_set(fingerprint(profile))
    if existing is None:
        _text_encoder(profile, store, config, encoder).check_ready()
    items = [{"item_id": "prototypes", "photo_id": None, "manifest": manifest,
              "input_fingerprint": fingerprint(manifest), "action": "reuse" if existing else "compute",
              "result_id": existing["set_id"] if existing else None, "reason": None}]
    return _save_plan(items, profile, "prototypes", store=store, persist=persist)


def _participants(ids, profile, store, metric):
    participants = []
    for pid in _ids(ids, store):
        photo = store.photo(pid)
        if photo["ingest_state"] != "available":
            raise PhotographyError("FEATURE_INVALID_INPUT", "Reingest invalid photos before comparing.")
        row = {"photo_id": pid, "content_version": photo["content_version"], "result_id": None}
        if metric == "hamming":
            entry, record = inspect_feature(pid, profile, store=store)
            if record is None:
                raise PhotographyError("FEATURE_DEPENDENCY_MISSING", "Build current fingerprints before comparing.",
                                       details={"photo_id": pid, "status": entry["status"]})
            row.update(result_id=record["result_id"], input_fingerprint=record["input_fingerprint"],
                       payload_hash=record["payload_hash"])
        participants.append(row)
    return participants


def create_compare_plan(ids, *, store, config, profile, metric="hamming", max_distance=8, persist=True):
    if profile["component"] != "perceptual_hash" or metric not in ("hamming", "exact"):
        raise PhotographyError("INVALID_ARGUMENT", "Choose fingerprint comparison metric hamming or exact.")
    if type(max_distance) is not int or not 0 <= max_distance <= 64:
        raise PhotographyError("INVALID_ARGUMENT", "Hamming distance must be an integer from 0 to 64.")
    if metric == "exact":
        max_distance = 0
    with store.read_snapshot():
        participants = _participants(ids, profile, store, metric)
    if len(participants) < 2:
        raise PhotographyError("NO_PHOTOS_SELECTED", "Comparison requires at least two photos.")
    manifest = {"participants": participants, "metric": metric, "max_distance": max_distance}
    item = {"item_id": "compare", "photo_id": None, "manifest": manifest,
            "input_fingerprint": fingerprint(manifest), "action": "compute", "result_id": None, "reason": None}
    return _save_plan([item], profile, "compare", store=store, persist=persist,
                      options={"metric": metric, "max_distance": max_distance,
                               "comparison_count": len(participants) * (len(participants) - 1) // 2})


def job(store, run_id):
    saved = store.feature_run(run_id)
    plan = saved["plan"]
    try:
        if (plan["schema"] != "image-feature-plan-v1" or plan["run_id"] != run_id
                or plan["album_id"] != store.album()["id"]
                or fingerprint({key: value for key, value in plan.items() if key != "digest"}) != plan["digest"]
                or profile_identity(plan["profile"])[0] != plan["profile_id"]
                or store.feature_profile(plan["profile_id"]) != plan["profile"]
                or plan["work_kind"] not in ("extract", "prototypes", "compare")
                or saved["confirmed_digest"] not in (None, plan["digest"])):
            raise ValueError("Plan identity changed.")
        items = store.feature_items(run_id)
        expected = {item["item_id"]: item for item in plan["items"]}
        if len(expected) != len(plan["items"]) or set(expected) != {item["item_id"] for item in items}:
            raise ValueError("Item scope changed.")
        for item in items:
            snapshot = expected[item["item_id"]]
            if (item["snapshot"] != snapshot or item["status"] not in STATES
                    or snapshot["action"] not in ("reuse", "compute", "skip")
                    or fingerprint(snapshot["manifest"]) != snapshot["input_fingerprint"]):
                raise ValueError("Frozen input changed.")
        if plan["counts"] != {"total": len(items), **{
                key: sum(item["action"] == key for item in plan["items"]) for key in ("compute", "reuse", "skip")}}:
            raise ValueError("Plan counts changed.")
    except (KeyError, TypeError, ValueError) as exc:
        raise PhotographyError("FEATURE_PLAN_INVALID", "Saved feature plan failed validation.") from exc
    counts = {"total": len(items), **{key: sum(item["status"] == key for item in items) for key in STATES}}
    return {**plan, "items": items, "planned_counts": plan["counts"], "counts": counts,
            "status": saved["status"], "confirmed": saved["confirmed_digest"] == plan["digest"],
            "confirmed_at": saved["confirmed_at"], "updated_at": saved["updated_at"],
            "model_calls": saved["model_calls"], "album": store.album()}


def _check_provider(provider, profile):
    if profile_identity(provider.profile())[0] != fingerprint(profile):
        raise PhotographyError("FEATURE_PROFILE_MISMATCH", "The provider does not match the frozen profile.")


def _text_encoder(profile, store, config, encoder=None):
    from .image_embedding_profiles import default_model_dir
    from .siglip_embedding import SiglipEncoder

    embedding_id = profile["dependencies"]["image_embedding"]
    source_profile = store.embedding_profile(embedding_id)
    runtime = encoder or SiglipEncoder(default_model_dir(config.model_cache_root), profile=source_profile)
    if fingerprint(runtime.profile()) != embedding_id:
        raise PhotographyError("FEATURE_PROFILE_MISMATCH", "Scene text encoder does not match its image embedding.")
    return runtime


def _finish(store, run_id, item, status, result_id=None, error=None):
    item.update(status=status, result_id=result_id, error=error)
    if item["attempts"] and item["attempts"][-1].get("completed_at") is None:
        item["attempts"][-1].update(status=status, completed_at=now(), error=error)
    store.update_feature_item(run_id, item)
    store.release_feature_input(run_id, item["item_id"])


@contextmanager
def _item_transaction(store, item):
    fields = ("status", "attempts", "error", "result_id", "progress")
    before = {key: deepcopy(item[key]) for key in fields}
    committed = False
    try:
        with store.transaction():
            yield
        committed = True
    finally:
        if not committed:
            item.update(before)


def _model_call(store, saved, item):
    with _item_transaction(store, item):
        item["attempts"][-1]["model_called"] = True
        store.update_feature_item(saved["run_id"], item)
        store.update_feature_run(saved["run_id"], model_calls=saved["model_calls"] + 1)
    saved["model_calls"] += 1


class _CountingEncoder:
    def __init__(self, encoder, store, saved, item):
        self.encoder, self.store, self.saved, self.item = encoder, store, saved, item

    def profile(self):
        return self.encoder.profile()

    def encode_text(self, text):
        _model_call(self.store, self.saved, self.item)
        return self.encoder.encode_text(text)


def _compute_extraction(item, saved, *, store, config, provider):
    from . import feature_algorithms

    profile = saved["profile"]
    manifest = item["snapshot"]["manifest"]
    data = load_input(item["photo_id"], profile, manifest, store=store)
    component = profile["component"]
    if component in ("ocr", "objects"):
        _check_provider(provider, profile)
        _model_call(store, saved, item)
        payload = provider.compute(data)
        _check_provider(provider, profile)
        metadata = getattr(provider, "last_metadata", None)
        if metadata is not None:
            if (not isinstance(metadata, dict)
                    or any(type(metadata.get(key)) is not int or metadata[key] < 0
                           for key in ("onnx_calls", "total_count", "returned_count"))
                    or type(metadata.get("truncated")) is not bool):
                raise PhotographyError("FEATURE_RESULT_INVALID", "Invalid provider execution counters.")
            item["attempts"][-1]["provider_metadata"] = {
                key: metadata[key] for key in ("onnx_calls", "total_count", "returned_count", "truncated")}
    elif component == "color":
        payload = feature_algorithms.compute_color(data, profile)
    elif component == "perceptual_hash":
        payload = feature_algorithms.compute_hash(data, profile)
    elif component == "composition":
        payload = feature_algorithms.compute_composition(data, profile)
    else:
        payload = feature_algorithms.compute_scene(data[0], data[1], profile)
    payload = validate_payload(profile, payload)
    if component in ("ocr", "objects", "color", "composition") and (
            payload["width"], payload["height"]) != (manifest["width"], manifest["height"]):
        raise PhotographyError("FEATURE_RESULT_INVALID", "Provider output coordinates identify the wrong input size.")
    with _item_transaction(store, item):
        verify_manifest(item["photo_id"], profile, manifest, store=store)
        result_id = store.put_feature_result(
            item["photo_id"], saved["profile_id"], manifest, payload,
            feature_dependencies=[manifest["source_result_id"]] if component == "composition" else [],
            embedding_dependencies=[manifest["embedding_result_id"]] if component == "scene" else [])
        _finish(store, saved["run_id"], item, "computed", result_id)


def _verify_compare(saved, store):
    manifest = saved["items"][0]["snapshot"]["manifest"]
    current = _participants([row["photo_id"] for row in manifest["participants"]],
                            saved["profile"], store, manifest["metric"])
    if current != manifest["participants"]:
        raise PhotographyError("FEATURE_INPUT_CHANGED", "Comparison inputs changed; prepare a new comparison.")
    return current


def _compute_compare(item, saved, store):
    from .feature_algorithms import hash_distance

    with store.read_snapshot():
        participants = _verify_compare(saved, store)
        payloads = {row["result_id"]: store.feature_result(row["result_id"])["payload"]
                    for row in participants if row["result_id"] is not None}
        checkpoint = store.prepare_similarity_checkpoint(saved["run_id"])
    metric = saved["options"]["metric"]
    threshold = saved["options"]["max_distance"]
    completed = item["progress"].get("comparisons", 0)
    if type(completed) is not int or not 0 <= completed <= saved["options"]["comparison_count"]:
        raise PhotographyError("FEATURE_PLAN_INVALID", "Invalid comparison resume cursor.")
    position = 0
    pairs = []
    endpoints = set()
    for i, left in enumerate(participants):
        for right in participants[i + 1:]:
            position += 1
            if position <= completed:
                continue
            endpoints.update((left["photo_id"], right["photo_id"]))
            distance = (int(left["content_version"] != right["content_version"]) if metric == "exact" else
                        hash_distance(payloads[left["result_id"]], payloads[right["result_id"]]))
            if distance <= threshold:
                pairs.append({"photo_id_a": left["photo_id"], "photo_id_b": right["photo_id"],
                              "result_id_a": left["result_id"], "result_id_b": right["result_id"],
                              "content_version_a": left["content_version"], "content_version_b": right["content_version"],
                              "metric": metric, "distance": distance})
            if position % 256 == 0:
                with _item_transaction(store, item):
                    store.put_similarity_checkpoint(checkpoint, pairs, position, endpoints)
                    item["progress"] = {"comparisons": position}
                pairs = []
                endpoints = set()
    with _item_transaction(store, item):
        _verify_compare(saved, store)
        store.put_similarity_checkpoint(checkpoint, pairs, position, endpoints)
        item["progress"] = {"comparisons": position}
        _finish(store, saved["run_id"], item, "computed")


def execute_plan(run_id, *, store, config, confirm=None, resume=False, confirm_stopped=False,
                 provider=None, encoder=None, python_path=None):
    if store.db.in_transaction:
        raise PhotographyError("FEATURE_TRANSACTION_ACTIVE", "Execute outside any active SQLite transaction.")
    if resume and confirm is not None or confirm_stopped and not resume:
        raise PhotographyError("INVALID_ARGUMENT", "Resume reuses prior confirmation; stopped applies only to resume.")
    with execution_lock(Path(str(store.database_path) + ".image-features.lock")), ExitStack() as resources:
        with store.transaction():
            saved = job(store, run_id)
            calls_before = saved["model_calls"]
            if confirm is not None and confirm != saved["digest"]:
                raise PhotographyError("FEATURE_CONFIRMATION_MISMATCH", "Confirmation does not match this feature plan.")
            if not saved["confirmed"] and (resume or confirm != saved["digest"]):
                raise PhotographyError("FEATURE_CONFIRMATION_REQUIRED", "Confirm the exact feature plan before execution.")
            if saved["status"] == "completed":
                return {**saved, "model_calls_this_execution": 0}
            if not resume and saved["status"] != "proposed":
                raise PhotographyError("FEATURE_RESUME_REQUIRED", "Use resume for an already-started feature run.")
            if saved["status"] == "running" and not confirm_stopped:
                raise PhotographyError("FEATURE_STOPPED_CONFIRMATION_REQUIRED", "Confirm prior execution stopped on all devices.")
            if confirm is not None:
                store.update_feature_run(run_id, confirmed_digest=confirm, confirmed_at=now())
            if saved["status"] == "running":
                for item in saved["items"]:
                    store.release_feature_input(run_id, item["item_id"])
            store.update_feature_run(run_id, status="running")
        profile = saved["profile"]
        try:
            for item in saved["items"]:
                inference_started = False
                try:
                    with _item_transaction(store, item):
                        snapshot = item["snapshot"]
                        if snapshot["action"] == "skip":
                            state = snapshot["reason"] if snapshot["reason"] in STATES else "invalid_input"
                            _finish(store, run_id, item, state, error=snapshot.get("error"))
                            continue
                        if saved["work_kind"] == "extract":
                            verify_manifest(item["photo_id"], profile, snapshot["manifest"], store=store)
                            existing = store.find_feature_result(item["photo_id"], saved["profile_id"],
                                                                 snapshot["input_fingerprint"])
                        elif saved["work_kind"] == "prototypes":
                            if _prototype_manifest(profile, store) != snapshot["manifest"]:
                                raise PhotographyError("FEATURE_INPUT_CHANGED", "Scene prototype inputs changed.")
                            existing = store.scene_prototype_set(saved["profile_id"])
                        else:
                            _verify_compare(saved, store)
                            existing = None
                        if existing is not None:
                            result_id = existing["result_id"] if saved["work_kind"] == "extract" else existing["set_id"]
                            _finish(store, run_id, item, "computed" if item["status"] == "computed" else "cached", result_id)
                            continue
                        if snapshot["action"] == "reuse":
                            raise PhotographyError("FEATURE_REPLAN_REQUIRED", "A planned cache hit is gone; do not expand into computation.")
                        store.claim_feature_input(run_id, item["item_id"], saved["profile_id"], snapshot["input_fingerprint"])
                        item["attempts"].append({"started_at": now(), "completed_at": None, "model_called": False})
                        item.update(status="running", error=None)
                        store.update_feature_item(run_id, item)
                    started = time.perf_counter()
                    if saved["work_kind"] == "compare":
                        _compute_compare(item, saved, store)
                    elif saved["work_kind"] == "prototypes":
                        from .feature_algorithms import make_scene_prototypes

                        inference_started = True
                        runtime = _text_encoder(profile, store, config, encoder)
                        prototypes = make_scene_prototypes(profile, _CountingEncoder(runtime, store, saved, item))
                        with _item_transaction(store, item):
                            if _prototype_manifest(profile, store) != snapshot["manifest"]:
                                raise PhotographyError("FEATURE_INPUT_CHANGED", "Scene prototype dependency changed.")
                            set_id = store.put_scene_prototypes(saved["profile_id"], prototypes)
                            _finish(store, run_id, item, "computed", set_id)
                    else:
                        if profile["component"] in ("ocr", "objects"):
                            inference_started = True
                            if provider is None:
                                provider = resources.enter_context(_vision_provider(profile, config, python_path))
                                provider.check_ready()
                        _compute_extraction(item, saved, store=store, config=config, provider=provider)
                    with _item_transaction(store, item):
                        item["attempts"][-1]["elapsed_seconds"] = time.perf_counter() - started
                        store.update_feature_item(run_id, item)
                except (PhotographyError, sqlite3.Error, OSError) as exc:
                    error = exc if isinstance(exc, PhotographyError) else PhotographyError("FEATURE_EXECUTION_FAILED", str(exc))
                    state = {"FEATURE_INPUT_CHANGED": "stale", "FEATURE_INPUT_UNAVAILABLE": "input_unavailable",
                             "FEATURE_INVALID_INPUT": "invalid_input",
                             "FEATURE_DEPENDENCY_MISSING": "dependency_missing"}.get(error.code, "failed")
                    with _item_transaction(store, item):
                        _finish(store, run_id, item, state, error=error.to_dict())
                    input_errors = {"FEATURE_INPUT_CHANGED", "FEATURE_INPUT_UNAVAILABLE", "FEATURE_INVALID_INPUT",
                                    "FEATURE_INPUT_INVALID", "INVALID_PREVIEW", "FEATURE_DEPENDENCY_MISSING"}
                    if ((inference_started and error.code not in input_errors)
                            or error.code in ("FEATURE_RUNTIME_MISMATCH", "FEATURE_PROFILE_MISMATCH",
                                              "FEATURE_PROFILE_INVALID")):
                        break
        except KeyboardInterrupt:
            with _item_transaction(store, item):
                if item["status"] == "running":
                    _finish(store, run_id, item, "cancelled", error={"code": "INTERRUPTED", "message": "Execution interrupted."})
                store.update_feature_run(run_id, status="cancelled")
            return {**job(store, run_id), "interrupted": True}
        with store.transaction():
            result = job(store, run_id)
            successes = result["counts"]["cached"] + result["counts"]["computed"]
            state = "completed" if successes == result["counts"]["total"] else "partial" if successes else "failed"
            store.update_feature_run(run_id, status=state)
        result = job(store, run_id)
        return {**result, "model_calls_this_execution": result["model_calls"] - calls_before}
