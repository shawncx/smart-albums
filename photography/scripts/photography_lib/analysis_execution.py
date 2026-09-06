"""Execute the exact confirmed snapshot; persist before sending and after each result."""
import math
import random
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from uuid import uuid4
from .analyze import prepare, assert_current
from .analysis_schema import fingerprint, validate_analysis
from .analysis_settings import make_provider
from .analysis_planner import require_confirmed
from .analysis_api import OpenAIHTTP, RequestFailure, build_payload, parse_results
from .config import PhotographyError
from .sqlite_storage import now

TERMINAL = {"analyzed", "cached", "failed", "stale", "cancelled"}


def snapshot_input(snapshot, store, config, profile):
    photo, preview, image_hash, key = prepare(snapshot["photo_id"], store, config, fingerprint(profile))
    if any(photo[k] != snapshot[k] for k in ("content_version", "thumbnail_profile")) or image_hash != snapshot["input_image_hash"]:
        raise PhotographyError("PHOTO_CHANGED", "The confirmed photo input changed; create a new plan for it.")
    return photo, preview


def group_key(snapshot, group, profile):
    if len(group) == 1:
        return snapshot["cache_key"], profile
    recipe = {**profile, "group_recipe": "labeled-photos-v1"}
    return fingerprint({"base": snapshot["cache_key"], "recipe": recipe,
        "group": [(s["photo_id"], s["input_image_hash"], s["content_version"]) for s in group]}), recipe


def build_requests(plan, store, config):
    """Create requests once. Existing requests preserve image grouping during recovery."""
    if store.requests(plan["plan_id"]):
        return
    pending = []
    for snapshot in plan["snapshots"]:
        if snapshot["status"] == "cached":
            try:
                snapshot_input(snapshot, store, config, plan["profile"])
            except PhotographyError as exc:
                store.save_item(plan["plan_id"], {**snapshot, "status": "stale", "error": exc.to_dict()})
        if snapshot["status"] != "pending":
            continue
        item = dict(snapshot)
        try:
            snapshot_input(snapshot, store, config, plan["profile"])
            cached = None if plan["force"] else store.cached_analysis(snapshot["photo_id"], snapshot["cache_key"])
            if cached:
                item.update(status="cached", analysis_id=cached["analysis_id"])
            else:
                pending.append(snapshot)
        except PhotographyError as exc:
            item.update(status="stale", error=exc.to_dict())
        store.save_item(plan["plan_id"], item)
    size = plan["execution"]["images_per_request"]
    for index in range(0, len(pending), size):
        group = pending[index:index + size]
        request = dict(id="request_" + uuid4().hex, plan_id=plan["plan_id"], snapshots=group,
            status="pending", attempts=[], created_at=now(), results={})
        store.save_request(request)


def prepare_request(plan, request, store, config):
    images, cached = [], []
    for snapshot in request["snapshots"]:
        _, preview = snapshot_input(snapshot, store, config, plan["profile"])
        images.append((snapshot["photo_id"], preview))
        key, _ = group_key(snapshot, request["snapshots"], plan["profile"])
        cached.append(None if plan["force"] else store.cached_analysis(snapshot["photo_id"], key))
    # Preserve multi-image context: reuse a group only if all outputs already exist.
    if all(cached):
        for snap, record in zip(request["snapshots"], cached):
            store.save_item(plan["plan_id"], {**snap, "status": "cached", "analysis_id": record["analysis_id"]})
        request["status"] = "completed"
        store.save_request(request)
        return None
    return images


def start_attempt(plan, request, store):
    keys = [s["cache_key"] for s in request["snapshots"]]
    request["status"] = "running"
    request["attempts"].append(dict(started_at=now(), status="running", usage=None))
    store.claim(request, keys)
    for snap in request["snapshots"]:
        store.save_item(plan["plan_id"], {**snap, "status": "running", "request_id": request["id"]})


def fail_request(plan, request, store, error, *, status="failed"):
    request["status"] = status
    request["error"] = error
    store.save_request(request)
    for snap in request["snapshots"]:
        existing = next((i for i in store.plan_items(plan["plan_id"]) if i["photo_id"] == snap["photo_id"]), snap)
        if existing["status"] != "analyzed":
            store.save_item(plan["plan_id"], {**existing, "status": status, "error": error})
    if status != "unknown":
        store.release(request)


def save_outputs(plan, request, store, config, outputs, errors, usage, elapsed=0):
    for snap in request["snapshots"]:
        pid = snap["photo_id"]
        if pid in request.get("results", {}):
            continue
        item = {**snap, "request_id": request["id"]}
        try:
            if pid in errors:
                raise PhotographyError(errors[pid]["code"], errors[pid]["message"])
            output = outputs[pid]
            validate_analysis(output.data)
            photo, _ = snapshot_input(snap, store, config, plan["profile"])
            key, profile = group_key(snap, request["snapshots"], plan["profile"])
            # Repeated collection must never replace a successful record.
            record = dict(analysis_id="analysis_" + uuid4().hex, photo_id=pid,
                content_version=snap["content_version"], input_image_hash=snap["input_image_hash"],
                thumbnail_profile=snap["thumbnail_profile"], cache_key=key,
                analysis_config_id=fingerprint(profile), analysis_profile=profile,
                model=output.model, response_id=output.response_id, usage=output.usage,
                model_source=output.model_source, usage_scope=output.usage_scope,
                request_id=request["id"], plan_id=plan["plan_id"],
                model_elapsed_seconds=elapsed, created_at=now(), data=output.data)
            current = assert_current(photo, snap["input_image_hash"], store, config)
            store.put_analysis(record)
            store.put_photo({**current, "needs_analysis": False})
            item.update(status="analyzed", analysis_id=record["analysis_id"])
        except PhotographyError as exc:
            item.update(status="stale" if exc.code in ("PHOTO_CHANGED", "PHOTO_UNAVAILABLE") else "failed", error=exc.to_dict())
        store.save_item(plan["plan_id"], item)
        request.setdefault("results", {})[pid] = item["status"]
    request["status"] = "completed"
    if request["attempts"]:
        request["attempts"][-1].update(status="completed", usage=usage, completed_at=now(), elapsed_seconds=elapsed)
    store.save_request(request)
    store.release(request)


def summarize(store, plan_id):
    plan = store.plan(plan_id)
    items, requests = store.plan_items(plan_id), store.requests(plan_id)
    counts = {s: sum(i["status"] == s for i in items) for s in
              ("cached", "pending", "analyzed", "failed", "stale", "running", "submitted", "unknown", "cancelled")}
    attempts = [a for r in requests for a in r["attempts"]]
    usage = {}
    for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens"):
        known = [a["usage"][key] for a in attempts if a.get("usage") and key in a["usage"]]
        usage[key] = sum(known) if known else None
    summary = dict(counts=counts, requests_started=sum(bool(r["attempts"]) for r in requests),
        attempt_count=len(attempts), retry_count=sum(max(0, len(r["attempts"]) - 1) for r in requests),
        known_usage=usage, unknown_usage_attempts=sum(not a.get("usage") for a in attempts),
        request_elapsed_seconds=round(sum(a.get("elapsed_seconds", 0) for a in attempts), 3),
        usage_note="Known counters only; cached input is part of input, multi-image usage belongs to the request. Codex usage includes turn context.")
    if not any(counts[s] for s in ("pending", "running", "submitted", "unknown")):
        plan["status"] = "partial" if counts["failed"] or counts["stale"] or counts["cancelled"] else "completed"
        plan["completed_at"] = now()
    elif counts["unknown"]:
        plan["status"] = "attention"
    plan["summary"] = summary
    with store.transaction():
        store.save_plan(plan)
        store.save_analysis_run(dict(run_id=plan_id, status=plan["status"], requested=len(items),
            analyzed=counts["analyzed"], cached=counts["cached"], failed=counts["failed"] + counts["stale"],
            started_at=plan.get("started_at"), completed_at=plan.get("completed_at"),
            photo_ids=[i["photo_id"] for i in items], results=items, execution_summary=summary,
            analysis_config_id=fingerprint(plan["profile"]), analysis_profile=plan["profile"]))
    return {**plan, "results": items, "requests": requests}


class RateGate:
    def __init__(self, settings, recent=()):
        self.settings = dict(settings)
        self.events = deque()
        self.cooldown = 0

    def delay(self, tokens):
        t = time.monotonic()
        while self.events and self.events[0][0] <= t - 60:
            self.events.popleft()
        s = self.settings
        if s["tpm"] and tokens > s["tpm"]:
            raise PhotographyError("REQUEST_OVER_TOKEN_LIMIT", "A request exceeds the configured token budget. Replan with fewer photos or a smaller output limit.")
        delay = max(0, self.cooldown - t)
        if self.events and ((s["rpm"] and len(self.events) >= s["rpm"]) or
                            (s["tpm"] and sum(v for _, v in self.events) + tokens > s["tpm"])):
            delay = max(delay, self.events[0][0] + 60 - t)
        return delay

    def reserve(self, tokens):
        self.events.append((time.monotonic(), tokens))

    def observe(self, headers):
        import re
        for kind, key in (("requests", "rpm"), ("tokens", "tpm")):
            try:
                limit = float(headers.get("x-ratelimit-limit-" + kind, "nan"))
                if math.isfinite(limit) and limit > 0 and (kind == "requests" or self.settings["input_tokens_per_photo"]):
                    self.settings[key] = min(self.settings[key] or limit, limit)
                remaining = float(headers.get("x-ratelimit-remaining-" + kind, "nan"))
                if remaining <= 0:
                    reset = headers.get("x-ratelimit-reset-" + kind, "")
                    parts = re.findall(r"([0-9.]+)(ms|s|m|h)", reset)
                    seconds = sum(float(v) * {"ms": .001, "s": 1, "m": 60, "h": 3600}[u] for v, u in parts)
                    self.cooldown = max(self.cooldown, time.monotonic() + seconds)
            except (ValueError, TypeError):
                pass


def call_immediate(plan, images, provider):
    started = time.perf_counter()
    try:
        if plan["settings"]["provider"] == "codex":
            result = provider.analyze(images[0][1])
            return {images[0][0]: result}, {}, result.usage, time.perf_counter() - started
        http = OpenAIHTTP(provider)
        envelope = http.call("/responses", method="POST", data=build_payload(provider.config, images))
        outputs, errors, usage = parse_results(envelope, [i[0] for i in images])
        return outputs, errors, usage, time.perf_counter() - started, http.headers
    except PhotographyError as exc:
        exc.elapsed_seconds = time.perf_counter() - started
        raise


def execute_plan(plan_id, *, store, config, provider=None, caller=None, resume=False, batch_http=None):
    initial = store.plan(plan_id)
    require_confirmed(initial)
    if initial["status"] in ("completed", "partial"):
        if not initial["pending"]:
            with store.transaction():
                build_requests(initial, store, config)
        return summarize(store, plan_id)
    with store.transaction():
        plan = store.plan(plan_id)
        require_confirmed(plan)
        if plan["status"] not in ("confirmed", "paused"):
            raise PhotographyError("PLAN_NOT_EXECUTABLE", "Plan is active or needs reconciliation. Inspect its status; do not submit it again.")
        if plan["status"] == "paused" and not resume:
            raise PhotographyError("RESUME_REQUIRED", "Use analysis-resume for this paused plan.")
        plan.update(status="running", started_at=plan.get("started_at") or now())
        store.save_plan(plan)
        build_requests(plan, store, config)
    provider = provider or make_provider(plan["settings"])
    if provider.profile() != plan["profile"]:
        with store.transaction():
            plan["status"] = "paused"
            store.save_plan(plan)
        raise PhotographyError("PROFILE_CHANGED", "Executor model configuration differs from the confirmed plan.")
    if plan["execution"]["mode"] == "batch":
        from .openai_batch import submit_batches
        return submit_batches(plan, store, config, provider, http=batch_http)
    caller = caller or call_immediate
    gate, inflight = RateGate(plan["settings"]), {}
    queue = deque(r for r in store.requests(plan_id) if r["status"] == "pending")
    # Resuming does not erase the last minute's reservations or a Retry-After deadline.
    from datetime import datetime
    for req in store.requests(plan_id):
        tokens = len(req["snapshots"]) * ((plan["settings"]["input_tokens_per_photo"] or 0) + plan["settings"]["max_output_tokens"])
        for attempt in req["attempts"]:
            age = time.time() - datetime.fromisoformat(attempt["started_at"]).timestamp()
            if age < 60:
                gate.events.append((time.monotonic() - age, tokens))
        gate.cooldown = max(gate.cooldown, time.monotonic() + max(0, req.get("retry_after_epoch", 0) - time.time()))
    gate.events = deque(sorted(gate.events))
    stop, waited = False, 0
    pool = ThreadPoolExecutor(max_workers=plan["execution"]["concurrency"])
    try:
        while queue or inflight:
            while queue and len(inflight) < plan["execution"]["concurrency"] and not stop:
                req = queue[0]
                tokens = len(req["snapshots"]) * ((plan["settings"]["input_tokens_per_photo"] or 0) + plan["settings"]["max_output_tokens"])
                try:
                    delay = gate.delay(tokens)
                    if delay:
                        if waited + delay > plan["settings"]["max_wait_seconds"]:
                            stop = True
                        elif not inflight:
                            time.sleep(min(delay, 1))
                            waited += min(delay, 1)
                        break
                    queue.popleft()
                    with store.transaction():
                        images = prepare_request(plan, req, store, config)
                        if images is None:
                            continue
                    # Login checks can start a child process; never hold a write
                    # transaction during them, and do not count them as model calls.
                    provider.check_ready()
                    with store.transaction():
                        start_attempt(plan, req, store)
                    gate.reserve(tokens)
                    inflight[pool.submit(caller, plan, images, provider)] = req
                except PhotographyError as exc:
                    if queue and queue[0]["id"] == req["id"]:
                        queue.popleft()
                    with store.transaction():
                        fail_request(plan, req, store, exc.to_dict(), status="stale" if exc.code == "PHOTO_CHANGED" else "failed")
                    if getattr(exc, "fatal", False):
                        stop = True
            if not inflight:
                if stop:
                    break
                continue
            done, _ = wait(inflight, timeout=1, return_when=FIRST_COMPLETED)
            for future in done:
                req = inflight.pop(future)
                try:
                    outcome = future.result()
                    outputs, errors, usage, elapsed = outcome[:4]
                    if len(outcome) > 4:
                        gate.observe(outcome[4])
                        req["rate_headers"] = outcome[4]
                    with store.transaction():
                        save_outputs(plan, req, store, config, outputs, errors, usage, elapsed)
                except PhotographyError as exc:
                    uncertain = getattr(exc, "uncertain", False) or exc.code in ("CODEX_TIMEOUT", "CODEX_EXEC_FAILED")
                    with store.transaction():
                        req["attempts"][-1].update(status="unknown" if uncertain else "failed", completed_at=now(),
                            error=exc.to_dict(), usage=getattr(exc, "usage", None), elapsed_seconds=getattr(exc, "elapsed_seconds", 0))
                        if getattr(exc, "retryable", False) and not uncertain and len(req["attempts"]) < plan["execution"]["max_attempts_per_request"]:
                            req["status"] = "pending"
                            store.save_request(req)
                            queue.appendleft(req)
                            delay = max(getattr(exc, "wait", 0), 2 ** (len(req["attempts"]) - 1) + random.random())
                            req["retry_after_epoch"] = time.time() + delay
                            store.save_request(req)
                            gate.cooldown = max(gate.cooldown, time.monotonic() + delay)
                        else:
                            fail_request(plan, req, store, exc.to_dict(), status="unknown" if uncertain else "failed")
                        if getattr(exc, "fatal", False) or uncertain:
                            stop = True
                except BaseException:
                    with store.transaction():
                        req["attempts"][-1]["status"] = "unknown"
                        fail_request(plan, req, store, {"code": "EXECUTION_UNCERTAIN", "message": "Request interrupted or executor failed; inspect before retrying."}, status="unknown")
                    stop = True
    finally:
        # Running calls are journaled before dispatch. An interruption leaves them
        # unresolved instead of automatically releasing a potentially billed input.
        pool.shutdown(wait=True, cancel_futures=True)
        with store.transaction():
            for req in inflight.values():
                req["attempts"][-1]["status"] = "unknown"
                fail_request(plan, req, store, {"code": "INTERRUPTED", "message": "Call may have completed; inspect before retrying."}, status="unknown")
            current = store.plan(plan_id)
            current["status"] = "paused"
            store.save_plan(current)
    return summarize(store, plan_id)
