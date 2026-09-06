"""Local, inspectable execution proposals. Planning never invokes a model."""
import math
from uuid import uuid4
from .analyze import prepare, unique_ids
from .analysis_schema import fingerprint
from .analysis_settings import resolve_settings, make_provider
from .provider_capabilities import capabilities
from .config import PhotographyError
from .sqlite_storage import now


def plan_digest(plan):
    return fingerprint({k: plan[k] for k in ("settings", "profile", "snapshots", "execution", "force")})


def create_plan(ids, *, store, config, settings=None, force=False, persist=True, provider=None, caps=None):
    ids = unique_ids(ids) if ids else []
    settings = resolve_settings(store.settings(), settings)
    provider = provider or make_provider(settings)
    profile = provider.profile()
    config_id = fingerprint(profile)
    caps = caps or capabilities(settings)
    snapshots = []
    for pid in ids:
        try:
            photo, preview, image_hash, key = prepare(pid, store, config, config_id)
            cached = None if force else store.cached_analysis(pid, key)
            history = store.analysis_records(pid)
            reason = ("forced" if force else "cache_match" if cached else "never_analyzed" if not history
                      else "input_changed" if all(r["content_version"] != photo["content_version"] or
                           r["input_image_hash"] != image_hash for r in history) else "configuration_changed")
            entry = dict(photo_id=pid, content_version=photo["content_version"],
                thumbnail_profile=photo["thumbnail_profile"], input_image_hash=image_hash, cache_key=key,
                preview_bytes=len(preview), status="cached" if cached else "pending", reason=reason)
            if cached:
                entry["analysis_id"] = cached["analysis_id"]
        except PhotographyError as exc:
            entry = dict(photo_id=pid, status="failed", error=exc.to_dict())
        snapshots.append(entry)
    counts = {k: sum(i["status"] == k for i in snapshots) for k in ("cached", "pending", "failed")}
    pending = counts["pending"]
    warnings = []
    reasons = []
    mode = settings["mode"]
    if mode == "auto":
        # This is a configurable product preference, not an API threshold.
        many = pending >= 50 or (settings["rpm"] and pending / settings["rpm"] >= 5)
        mode = "batch" if caps["batch"] and settings["wait_preference"] == "flexible" and many else "immediate"
        reasons.append("Flexible wait and substantial remaining work favor Batch." if mode == "batch"
                       else "Immediate results or small/unknown workload favor immediate processing.")
    if mode == "batch" and not caps["batch"]:
        raise PhotographyError("MODE_UNSUPPORTED", "This channel/model/endpoint has no verified Batch adapter. Choose immediate or another model explicitly.")
    images = settings["images_per_request"]
    if images > 1 and (not caps["multi_image"] or mode == "batch"):
        raise PhotographyError("MULTI_IMAGE_UNSUPPORTED", "Multi-image is optional for supported immediate API models only; Batch uses one photo per request.")
    if images > 1:
        warnings.append("Experimental multi-image recipe: quality unverified, usage is per request, and caches are specific to the complete image group.")
        from .analysis_execution import group_key
        candidates = [s for s in snapshots if s["status"] == "pending"]
        for start in range(0, len(candidates), images):
            group = candidates[start:start + images]
            cached_group = [None if force else store.cached_analysis(s["photo_id"], group_key(s, group, profile)[0]) for s in group]
            if all(cached_group):
                for snap, record in zip(group, cached_group):
                    snap.update(status="cached", analysis_id=record["analysis_id"], reason="group_cache_match")
        counts = {k: sum(i["status"] == k for i in snapshots) for k in ("cached", "pending", "failed")}
        pending = counts["pending"]
    if settings["input_tokens_per_photo"] and caps.get("context_tokens"):
        capacity = math.floor(caps["context_tokens"] / (settings["input_tokens_per_photo"] + settings["max_output_tokens"]))
        if capacity < images:
            raise PhotographyError("CONTEXT_LIMIT", "Requested group exceeds the estimated context budget; reduce photos per request.")
    concurrency = settings["concurrency"]
    if settings["provider"] == "codex" or mode == "batch" or not (settings["rpm"] and settings["tpm"] and settings["input_tokens_per_photo"]):
        concurrency = 1
    if concurrency != settings["concurrency"]:
        warnings.append("Concurrency reduced to one: this channel or unknown request/token limits require conservative execution.")
    if settings["input_tokens_per_photo"] is None:
        warnings.append("Input tokens, cost and token-based throughput are unknown. Set an estimate from the selected model's metering rules; JPEG bytes are not tokens.")
    if mode == "batch":
        warnings.append("Batch uploads a file of JPEG previews and may take up to the provider's completion window. File cleanup is explicit after collection.")
    estimate = pending * settings["input_tokens_per_photo"] if settings["input_tokens_per_photo"] else None
    output_max = pending * settings["max_output_tokens"]
    cost = None
    if settings["provider"] == "openai" and estimate is not None and settings["input_price"] and settings["output_price"]:
        discount = .5 if mode == "batch" else 1
        cost = {"currency": "USD", "basis": "user_supplied_prices_per_million_and_input_estimate",
            "range": [round(discount * estimate * settings["input_price"] / 1e6, 6),
                      round(discount * (estimate * settings["input_price"] + output_max * settings["output_price"]) / 1e6, 6)],
            "excludes": "Retries, uncertainty in input tokens and other account charges; not a spending guarantee."}
    execution = dict(mode=mode, images_per_request=images, concurrency=concurrency,
        initial_requests=math.ceil(pending / images), max_attempts_per_request=1 + settings["retries"],
        reasons=reasons, warnings=warnings, estimates=dict(input_tokens=estimate,
            output_tokens_ceiling=output_max, cost=cost, elapsed_seconds=None,
            batch_window_hours=24 if mode == "batch" else None))
    plan = dict(plan_id="plan_" + uuid4().hex, created_at=now(), status="proposed" if pending else "completed",
        requested=len(ids), **counts, force=force, settings=settings, profile=profile,
        capabilities=caps, snapshots=snapshots, execution=execution, model_calls=0, confirmed_digest=None)
    plan["digest"] = plan_digest(plan)
    if persist:
        with store.transaction():
            store.save_plan(plan)
            for item in snapshots:
                store.save_item(plan["plan_id"], item)
    return plan


def confirm_plan(store, plan_id, digest):
    with store.transaction():
        plan = store.plan(plan_id)
        if digest != plan["digest"] or plan_digest(plan) != digest:
            raise PhotographyError("PLAN_CHANGED", "The proposal changed. Review and confirm its current digest.")
        if plan["status"] not in ("proposed", "confirmed", "completed"):
            raise PhotographyError("PLAN_STARTED", "This plan has already started. Inspect or resume it.")
        plan["confirmed_digest"] = digest
        plan["confirmed_at"] = now()
        if plan["pending"]:
            plan["status"] = "confirmed"
        store.save_plan(plan)
    return plan


def require_confirmed(plan):
    if plan["pending"] and (plan.get("confirmed_digest") != plan["digest"] or plan_digest(plan) != plan["digest"]):
        raise PhotographyError("CONFIRMATION_REQUIRED", "Review the saved proposal and confirm its digest before execution.")
