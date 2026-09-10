"""Explicit, confirmed review of frozen saved previews; never original files."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timezone
import time
from uuid import uuid4

from .config import PhotographyError
from .fingerprints import fingerprint
from .index_lock import execution_lock
from .review_schema import build_prompt, parse_response, review_profile, validate_profile
from .thumbnails import stored_preview


PLAN_VERSION = "ai-review-plan-v1"
DISCLOSURE = (
    "Send only the selected saved JPEG previews to GitHub Copilot using the current Copilot CLI login. "
    "This is cloud processing and may consume usage. One confirmation authorizes this task's frozen "
    "batches, including authentication and model checks, not later retries. Original files, EXIF and "
    "filesystem paths are not sent. Preview quality limits technical judgments. Scores are subjective "
    "and model versions/batch context can affect them. SDK attempts are not an exact billing count."
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(plan):
    return fingerprint({key: value for key, value in plan.items() if key != "digest"})


def _input(photo_id, store, *, pixels=False):
    photo = store.photo(photo_id)
    try:
        thumbnail = store.thumbnail(photo_id, include_data=False)
        if (photo["ingest_state"] != "available"
                or thumbnail["content_version"] != photo["content_version"]
                or thumbnail["profile"] != photo["thumbnail_profile"]
                or thumbnail["mime_type"] != "image/jpeg"
                or any(type(thumbnail[key]) is not int or thumbnail[key] < 1
                       for key in ("width", "height", "size_bytes"))):
            raise PhotographyError("INVALID_PREVIEW", "Saved preview does not match the photo.")
        data = stored_preview(photo, store) if pixels else None
        if data is not None and len(data) != thumbnail["size_bytes"]:
            raise PhotographyError("INVALID_PREVIEW", "Saved preview size is inconsistent.")
    except PhotographyError as exc:
        raise PhotographyError("REVIEW_INVALID_INPUT", "Repair or reingest the selected photo's preview.",
                               details={"photo_id": photo_id, "cause": exc.code}) from exc
    manifest = {
        "input_scope": "stored_thumbnail", "content_version": photo["content_version"],
        "thumbnail_profile": thumbnail["profile"], "input_image_hash": thumbnail["image_hash"],
        "width": thumbnail["width"], "height": thumbnail["height"], "size_bytes": thumbnail["size_bytes"],
    }
    return manifest, data


def create_plan(ids, *, store, model, language="zh-CN", batch_size=4, max_concurrency=5,
                force=False, persist=True):
    if (not isinstance(ids, (list, tuple)) or not ids
            or any(not isinstance(key, str) or not key.strip() for key in ids)):
        raise PhotographyError("INVALID_ARGUMENT", "Select a nonempty array of existing photo IDs.")
    if type(batch_size) is not int or batch_size < 1:
        raise PhotographyError("INVALID_ARGUMENT", "Review batch size must be a positive integer.")
    if type(max_concurrency) is not int or max_concurrency < 1:
        raise PhotographyError("INVALID_ARGUMENT", "Review concurrency must be a positive integer.")
    if type(force) is not bool:
        raise PhotographyError("INVALID_ARGUMENT", "Fresh-review selection must be a boolean.")
    profile = review_profile(model, language)
    profile_id = fingerprint(profile)
    items = []
    with store.read_snapshot():
        for photo_id in dict.fromkeys(ids):
            manifest, _ = _input(photo_id, store, pixels=True)
            input_id = fingerprint(manifest)
            cached = None if force else store.find_review_result(photo_id, profile_id, input_id)
            items.append({"photo_id": photo_id, "input_manifest": manifest, "input_fingerprint": input_id,
                          "action": "reuse" if cached else "review",
                          "result_id": cached["result_id"] if cached else None})
        album_id = store.album()["id"]
    pending = [item for item in items if item["action"] == "review"]
    batches = [
        {"batch_id": "review_batch_" + uuid4().hex, "ordinal": offset // batch_size,
         "items": deepcopy(pending[offset:offset + batch_size])}
        for offset in range(0, len(pending), batch_size)
    ]
    plan = {
        "version": PLAN_VERSION, "run_id": "review_" + uuid4().hex, "album_id": album_id,
        "created_at": _now(), "profile": profile, "profile_id": profile_id,
        "batch_size": batch_size, "max_concurrency": max_concurrency, "force": force, "selection_count": len(ids),
        "duplicates_removed": len(ids) - len(items), "items": items, "batches": batches,
        "counts": {"total": len(items), "cached": len(items) - len(pending),
                   "pending": len(pending), "batches": len(batches)},
        "max_image_bytes": max((item["input_manifest"]["size_bytes"] for item in pending), default=0),
        "disclosure": DISCLOSURE,
    }
    plan["digest"] = _digest(plan)
    if persist:
        with store.transaction():
            _verify_inputs(plan, store)
            store.create_review_run(plan)
    return {**plan, "status": "planned" if persist else "dry_run", "request_attempts": 0,
            "provider_calls_this_operation": 0, "album": store.album()}


def _validate_run(run, batches, store):
    plan = run["plan"]
    try:
        validate_profile(plan["profile"])
        if (plan["version"] != PLAN_VERSION or plan["run_id"] != run["run_id"]
                or plan["album_id"] != store.album()["id"]
                or plan["digest"] != run["digest"] or _digest(plan) != run["digest"]
                or fingerprint(plan["profile"]) != plan["profile_id"]
                or type(plan["batch_size"]) is not int or plan["batch_size"] < 1
                or type(plan.get("max_concurrency", 1)) is not int or plan.get("max_concurrency", 1) < 1
                or type(plan["force"]) is not bool
                or run["confirmed_digest"] not in (None, plan["digest"])):
            raise ValueError("Plan identity mismatch.")
        items = plan["items"]
        if not items or len({item["photo_id"] for item in items}) != len(items):
            raise ValueError("Invalid selection.")
        pending = []
        for item in items:
            if (set(item) != {"photo_id", "input_manifest", "input_fingerprint", "action", "result_id"}
                    or fingerprint(item["input_manifest"]) != item["input_fingerprint"]
                    or item["action"] not in ("review", "reuse")
                    or (item["action"] == "review") != (item["result_id"] is None)):
                raise ValueError("Invalid snapshot.")
            if item["action"] == "review":
                pending.append(item)
        expected_counts = {"total": len(items), "pending": len(pending), "cached": len(items) - len(pending),
                           "batches": (len(pending) + plan["batch_size"] - 1) // plan["batch_size"]}
        if (plan["counts"] != expected_counts or len(batches) != expected_counts["batches"]
                or len(plan["batches"]) != len(batches)
                or plan["max_image_bytes"] != max(
                    (item["input_manifest"]["size_bytes"] for item in pending), default=0)):
            raise ValueError("Plan counts mismatch.")
        for ordinal, (batch, frozen) in enumerate(zip(batches, plan["batches"])):
            expected_items = pending[ordinal * plan["batch_size"]:(ordinal + 1) * plan["batch_size"]]
            if (batch["batch_id"] != frozen["batch_id"] or batch["run_id"] != run["run_id"]
                    or batch["ordinal"] != ordinal or frozen["ordinal"] != ordinal
                    or batch["items"] != expected_items or frozen["items"] != expected_items):
                raise ValueError("Batch scope mismatch.")
    except (KeyError, TypeError, ValueError, PhotographyError) as exc:
        raise PhotographyError("REVIEW_PLAN_INVALID", "Saved review plan failed its integrity check.") from exc
    return plan


def _retry_digest(run, batches):
    return fingerprint({
        "version": "ai-review-retry-v1", "run_id": run["run_id"], "digest": run["digest"],
        "revision": run["revision"], "request_attempts": run["request_attempts"],
        "unfinished": [{"batch_id": batch["batch_id"], "status": batch["status"], "attempts": batch["attempts"]}
                       for batch in batches if batch["status"] != "completed"],
    })


def job(store, run_id):
    with store.read_snapshot():
        run = store.review_run(run_id)
        batches = store.review_batches(run_id)
        plan = _validate_run(run, batches, store)
        records = store.review_run_results(run_id)
        cached = []
        for item in plan["items"]:
            if item["action"] == "reuse":
                record = store.review_result(item["result_id"])
                if (record["photo_id"] != item["photo_id"] or record["profile_id"] != plan["profile_id"]
                        or record["input_fingerprint"] != item["input_fingerprint"]):
                    raise PhotographyError("REVIEW_PLAN_INVALID", "Saved cached-result identity does not match the plan.")
                cached.append(record)
        for batch in batches:
            saved_ids = {record["photo_id"] for record in records if record["batch_id"] == batch["batch_id"]}
            expected_ids = {item["photo_id"] for item in batch["items"]} if batch["status"] == "completed" else set()
            if saved_ids != expected_ids:
                raise PhotographyError("REVIEW_PLAN_INVALID", "Saved review batch completion is inconsistent.")
        summary = {
            "total": len(plan["items"]), "cached": len(cached), "reviewed": len(records),
            "remaining": sum(len(batch["items"]) for batch in batches if batch["status"] != "completed"),
            "completed_batches": sum(batch["status"] == "completed" for batch in batches),
            "total_batches": len(batches),
            "review_status_counts": {
                status: sum(record["payload"].get("review_status", "reviewed") == status
                            for record in (*cached, *records))
                for status in ("reviewed", "partial", "unreviewable")
            },
        }
        retry = None
        if run["status"] not in ("planned", "completed"):
            retry = {"digest": _retry_digest(run, batches), "remaining_photos": summary["remaining"],
                     "remaining_batches": len(batches) - summary["completed_batches"],
                     "possible_duplicate_charge": any(batch["attempts"] for batch in batches
                                                       if batch["status"] != "completed"),
                     "confirmation_required": True, "confirm_stopped_required": run["status"] == "running"}
        return {
            **deepcopy(plan), "status": run["status"], "revision": run["revision"],
            "request_attempts": run["request_attempts"], "confirmed": run["confirmed_digest"] == plan["digest"],
            "elapsed_seconds": run["elapsed_seconds"],
            "approval_history": run["approval_history"], "batches": batches, "summary": summary,
            "results": [*cached, *records], "retry": retry, "interrupted": run["status"] == "interrupted",
            "provider_calls_this_operation": 0, "album": store.album(),
        }


def _verify_inputs(plan, store, *, pixels=False):
    for item in plan["items"]:
        current, _ = _input(item["photo_id"], store, pixels=pixels)
        if fingerprint(current) != item["input_fingerprint"]:
            raise PhotographyError("REVIEW_INPUT_CHANGED", "A selected preview changed; create a new review plan.",
                                   details={"photo_id": item["photo_id"]})


def _provider():
    from .copilot_review import CopilotReviewProvider
    return CopilotReviewProvider()


def execute_plan(run_id, *, store, confirm=None, resume=False, confirm_stopped=False, provider_factory=None):
    started = time.perf_counter()
    store.assert_writable()
    if store.db.in_transaction:
        raise PhotographyError("REVIEW_TRANSACTION_ACTIVE", "Review execution requires no active database transaction.")
    if not isinstance(confirm, str) or not confirm:
        raise PhotographyError("REVIEW_CONFIRMATION_REQUIRED", "Confirm the whole task before contacting Copilot.")
    if confirm_stopped and not resume:
        raise PhotographyError("INVALID_ARGUMENT", "Stopped-worker confirmation applies only to resume.")
    lock = store.database_path.with_name("." + store.database_path.name + ".review.lock")
    with execution_lock(lock, error_prefix="REVIEW", component="photo-review"):
        with store.read_snapshot():
            saved = job(store, run_id)
            if saved["status"] == "completed":
                if confirm != saved["digest"]:
                    raise PhotographyError("REVIEW_CONFIRMATION_MISMATCH", "Confirmation does not match this task.")
                return saved
            if resume:
                if saved["retry"] is None or confirm != saved["retry"]["digest"]:
                    raise PhotographyError("REVIEW_CONFIRMATION_MISMATCH", "Review and confirm the current retry scope.")
                if saved["status"] == "running" and not confirm_stopped:
                    raise PhotographyError("REVIEW_STOPPED_CONFIRMATION_REQUIRED",
                                           "Confirm that the earlier worker has stopped on every device.")
            elif saved["status"] != "planned":
                raise PhotographyError("REVIEW_RESUME_REQUIRED", "Use resume with a new explicit retry confirmation.")
            elif confirm != saved["digest"]:
                raise PhotographyError("REVIEW_CONFIRMATION_MISMATCH", "Confirmation does not match the frozen task.")
            plan = store.review_run(run_id)["plan"]
            previous_elapsed = saved["elapsed_seconds"]
            if fingerprint(review_profile(plan["profile"]["model"], plan["profile"]["language"])) != plan["profile_id"]:
                raise PhotographyError("REVIEW_PROFILE_CHANGED", "The bundled review configuration changed; create a new plan.")
            _verify_inputs(plan, store, pixels=True)
        with store.transaction():
            _verify_inputs(plan, store)
            approval = {"digest": confirm, "kind": "retry" if resume else "execute",
                        "revision": saved["revision"], "confirmed_at": _now()}
            if resume:
                approval["previous_attempts"] = [
                    {"batch_id": batch["batch_id"], "ordinal": batch["ordinal"], "attempts": batch["attempts"],
                     "status": batch["status"], "metadata": deepcopy(batch["metadata"]),
                     "error": deepcopy(batch["error"])}
                    for batch in saved["batches"] if batch["status"] != "completed" and batch["attempts"]
                ]
            approvals = [*saved["approval_history"], approval]
            store.update_review_run(run_id, status="running", confirmed_digest=plan["digest"],
                                    revision=saved["revision"] + 1, approval_history=approvals)
        return _execute_batches(plan, saved, store, provider_factory, started, previous_elapsed)


def _execute_batches(plan, saved, store, provider_factory, started, previous_elapsed):
    """Keep SQLite on the caller thread; only provider I/O runs in the bounded pool.

    Stop scheduling on the first observed failure, then settle every in-flight
    batch before releasing the album lock or making its retry scope available.
    Plans saved before concurrency was configurable retain serial execution.
    """
    from .review_provider import ReviewImage, ReviewRequest

    run_id = plan["run_id"]
    pending = [batch for batch in saved["batches"] if batch["status"] != "completed"]
    concurrency = min(plan.get("max_concurrency", 1), len(pending) or 1)
    provider = None
    attempts = provider_calls = next_batch = 0
    in_flight = {}
    first_error = None
    interrupted = False

    def remember_error(exc):
        nonlocal first_error, interrupted
        stopped = isinstance(exc, KeyboardInterrupt) or (
            isinstance(exc, PhotographyError) and exc.code in ("REVIEW_INTERRUPTED", "REVIEW_CANCELLED", "INTERRUPTED"))
        error = exc.to_dict() if isinstance(exc, PhotographyError) else {
            "code": "INTERRUPTED" if stopped else "REVIEW_EXECUTION_FAILED",
            "message": "Review interrupted." if stopped else "Review processing or storage failed.",
        }
        if first_error is None:
            first_error = error
        interrupted = interrupted or stopped
        return error, stopped

    def fail_batch(batch, exc, metadata, batch_started, diagnostics=None):
        error, stopped = remember_error(exc)
        if diagnostics:
            error["details"] = {**error.get("details", {}), "provider_response": diagnostics}
        if isinstance(exc, PhotographyError) and isinstance(exc.details, dict):
            usage = exc.details.get("usage")
            if isinstance(usage, dict):
                metadata.update(usage)
        with store.transaction():
            state = "interrupted" if stopped else "stale" if error["code"] == "REVIEW_INPUT_CHANGED" else "failed"
            store.update_review_batch(batch["batch_id"], status=state, error=error,
                                      metadata={**metadata, "elapsed_seconds": time.perf_counter() - batch_started})
            run = store.review_run(run_id)
            # Keep the run writable while other submitted batches are settling.
            store.update_review_run(run_id, revision=run["revision"] + 1,
                                    elapsed_seconds=previous_elapsed + time.perf_counter() - started)

    def finish_batch(future):
        batch, images, batch_started = in_flight.pop(future)
        metadata = {}
        diagnostics = {}
        try:
            reply = future.result()
            metadata = {**reply.metadata, "elapsed_seconds": time.perf_counter() - batch_started}
            diagnostics = getattr(reply, "diagnostics", {})
            payloads = parse_response(reply.text, [image.image_id for image in images])
            with store.transaction():
                _verify_inputs(plan, store)
                for image, item in zip(images, batch["items"]):
                    store.put_review_result(item["photo_id"], run_id, batch["batch_id"], plan["profile"],
                                            item["input_manifest"], payloads[image.image_id], metadata)
                store.update_review_batch(batch["batch_id"], status="completed", error=None, metadata=metadata)
                run = store.review_run(run_id)
                store.update_review_run(run_id, revision=run["revision"] + 1,
                                        elapsed_seconds=previous_elapsed + time.perf_counter() - started)
        except (Exception, KeyboardInterrupt) as exc:
            fail_batch(batch, exc, metadata, batch_started, diagnostics)

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="photo-review") as executor:
        while in_flight or (first_error is None and next_batch < len(pending)):
            try:
                # Consume every available result before refilling, including
                # failures from later batches that finished ahead of earlier ones.
                done = [future for future in in_flight if future.done()]
                if done:
                    for future in done:
                        finish_batch(future)
                    continue
                if first_error is None and next_batch < len(pending) and len(in_flight) < concurrency:
                    batch = pending[next_batch]
                    next_batch += 1
                    batch_started = time.perf_counter()
                    try:
                        with store.read_snapshot():
                            _verify_inputs(plan, store)
                            images = tuple(ReviewImage("image_" + str(index + 1),
                                                       _input(item["photo_id"], store, pixels=True)[1])
                                           for index, item in enumerate(batch["items"]))
                        request = ReviewRequest(
                            model=plan["profile"]["model"],
                            prompt=build_prompt(plan["profile"], [image.image_id for image in images]),
                            images=images, batch_size=plan["batch_size"], max_image_bytes=plan["max_image_bytes"])
                        with store.transaction():
                            _verify_inputs(plan, store)
                            run = store.review_run(run_id)
                            store.update_review_batch(batch["batch_id"], status="running",
                                                      attempts=batch["attempts"] + 1, error=None)
                            store.update_review_run(run_id, request_attempts=run["request_attempts"] + 1,
                                                    revision=run["revision"] + 1)
                        attempts += 1
                        if provider is None:
                            provider = (provider_factory or _provider)()
                        future = executor.submit(provider.review, request)
                        in_flight[future] = (batch, images, batch_started)
                        provider_calls += 1
                    except (Exception, KeyboardInterrupt) as exc:
                        fail_batch(batch, exc, {}, batch_started)
                elif in_flight:
                    wait(in_flight, return_when=FIRST_COMPLETED)
            except KeyboardInterrupt as exc:
                # A caller interrupt stops new sends, but cannot retract cloud
                # requests. Drain their results before exposing a retry digest.
                remember_error(exc)

    with store.transaction():
        run = store.review_run(run_id)
        successes = plan["counts"]["cached"] + len(store.review_run_results(run_id))
        status = ("interrupted" if interrupted else "partial" if successes else "failed") if first_error else "completed"
        store.update_review_run(run_id, status=status, revision=run["revision"] + 1,
                                elapsed_seconds=previous_elapsed + time.perf_counter() - started)
    return {**job(store, run_id), **({"error": first_error} if first_error else {}),
            "request_attempts_this_execution": attempts, "provider_calls_this_operation": provider_calls}


def _current_record(record, store):
    try:
        manifest, _ = _input(record["photo_id"], store, pixels=True)
    except PhotographyError as exc:
        if exc.code != "REVIEW_INVALID_INPUT":
            raise
        return {**record, "status": "invalid_input", "current": False, "input_error": exc.to_dict()}
    input_current = fingerprint(manifest) == record["input_fingerprint"]
    configured = review_profile(record["profile"]["model"], record["profile"]["language"])
    config_current = fingerprint(configured) == record["profile_id"]
    return {**record, "status": "ready" if input_current and config_current else "stale",
            "current": input_current and config_current,
            "input_current": input_current, "configuration_current": config_current}


def result(store, photo_id):
    with store.read_snapshot():
        store.photo(photo_id)
        records = store.review_history(photo_id, limit=1)
        record = _current_record(records[0], store) if records else None
    return {"photo_id": photo_id, "status": record["status"] if record else "missing",
            "result": record, "provider_calls_this_operation": 0}


def history(store, photo_id, *, limit=100, after=""):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise PhotographyError("INVALID_ARGUMENT", "Review history limit must be between 1 and 1000.")
    if not isinstance(after, str):
        raise PhotographyError("INVALID_ARGUMENT", "Review history cursor must be a string.")
    with store.read_snapshot():
        store.photo(photo_id)
        records = [_current_record(record, store) for record in store.review_history(photo_id, limit=limit, after=after)]
    return {"photo_id": photo_id, "results": records,
            "next_cursor": records[-1]["result_id"] if len(records) == limit else None,
            "provider_calls_this_operation": 0}
